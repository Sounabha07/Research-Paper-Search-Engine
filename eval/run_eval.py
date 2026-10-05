"""Eval suite.

  python /app/eval/run_eval.py [--ids 1,2,3] [--limit N] [--ablations baseline,no-keyword,no-vector,no-rerank]
                               [--no-keyword] [--no-vector] [--no-rerank] [--embedding MODEL] [--chunk-size N]
                               [--answers] [--sample 50]

Retrieval (always): each question goes through section-11 retrieval with the question itself as the only query
(no REWRITE), so retrieval metrics and every ablation make no Gemini calls. Metrics: recall@10 of the expected
papers in the selected papers, MRR, passage hit@10 (a passage from an expected paper among the top 10), and
retrieval latency p50/p95.

Ablations (patched in this process only; production code is unchanged). Variants combine with "+":
  --no-keyword    keyword channels off (papers, chunks, and the title part of the anchor channel)
  --no-vector     vector channels off (papers, chunks, and the vector part of the anchor channel)
  --no-rerank     no cross-encoder: candidates keep their fused (RRF) order
  fts             (in --ablations) the old keyword channel as it shipped (Postgres FTS, AND, ts_rank_cd; generic
                  words dropped; hits gated by the vector top 200) instead of BM25
  drop-generic    (in --ablations) generic question words dropped from the keyword query
  gate            (in --ablations) keyword hits kept only if in the vector channel's top 200
  kw-or           (in --ablations) experiment: keyword channel with OR between terms, ranked by ts_rank_cd
  kw-generic      (in --ablations) experiment: generic question words removed before the AND keyword query
  kw-gated        (in --ablations) experiment: keyword hits kept only if in the vector channel's top 200
  --embedding M   another embedding model; abstract and chunk embeddings are built once into schema
                  eval_emb_<model> (resumable; the whole corpus: hours for a base-size model)
  --chunk-size N  full-text papers re-chunked from parsed/ with target N tokens into schema eval_cs<N>;
                  passage retrieval reads that table (paper selection does not use chunks)

Answers (--answers): each question runs through the answer pipeline via the API (its mode; REWRITE included).
LLM responses come from the response cache when the prompt is unchanged, so reruns are free. Metrics: run latency
p50/p95 per mode, cost per run, removed-claims rate, writer fallbacks, recall@10 of the pipeline's selected papers,
and a sample of (sentence, cited passage) pairs in results/citation_sample_<timestamp>.csv for manual precision
checking.

Writes results/<timestamp>.json and results/<timestamp>.md.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import os
import random
import re
import statistics
import time
import urllib.request
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import retrieval.search as S
import yaml
from core.config import AppConfig, ChunkingConfig, get_config, get_settings
from core.db import raw_connection
from core.embeddings import abstract_text, embed_passages, load_embedder, load_reranker
from core.logging import setup_logging
from pgvector import HalfVector

EVAL_DIR = Path(__file__).parent
RESULTS = EVAL_DIR / "results"
API = os.environ.get("EVAL_API_URL", "http://api:8000")
K = 10


# ---- questions and metrics -------------------------------------------------------------------


def load_questions(ids: set[int] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    qs = yaml.safe_load((EVAL_DIR / "questions.yaml").read_text(encoding="utf-8"))["questions"]
    if ids:
        qs = [q for q in qs if q["id"] in ids]
    return qs[:limit] if limit else qs


def recall_at(expected: list[str], ranked: list[str], k: int = K) -> float:
    return len(set(expected) & set(ranked[:k])) / len(expected) if expected else 0.0


def reciprocal_rank(expected: list[str], ranked: list[str]) -> float:
    for i, a in enumerate(ranked, 1):
        if a in expected:
            return 1.0 / i
    return 0.0


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    i = (len(v) - 1) * p
    lo, hi = int(i), min(int(i) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


# ---- ablation patches ----------------------------------------------------------------------------


@contextlib.contextmanager
def patched(module: Any, **attrs: Any) -> Iterator[None]:
    old = {k: getattr(module, k) for k in attrs}
    for k, v in attrs.items():
        setattr(module, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(module, k, v)


async def _empty(*_: Any, **__: Any) -> list[Any]:
    return []


def anchor_variant(
    use_vector: bool, use_title: bool, vector_sql: str | None = None
) -> Callable[..., Awaitable[list[str]]]:
    """S.anchor_papers with one of its two parts switched off (or its vector part on another embedding table)."""

    async def anchor(
        conn: asyncpg.Connection, query: str, vec: HalfVector, filters: S.Filters, k: int, engine: str = "bm25"
    ) -> list[str]:
        lists = []
        if use_vector:
            if vector_sql:
                cond, params = filters.sql(3)
                lists.append([r["arxiv_id"] for r in await conn.fetch(vector_sql.format(cond=cond), vec, k, *params)])
            else:
                lists.append(await S.anchor_vector(conn, vec, filters, k))
        if use_title:
            lists.append(await S.anchor_title(conn, query, filters, k, engine))
        return [a for a, _ in S.rrf_fuse(lists, 60)[:k]]

    return anchor


_KW_ORIG = S.keyword_papers
_VEC_ORIG = S.vector_papers

# Question scaffolding and generic verbs/adjectives that Postgres' English stopword list keeps but that rarely appear
# in the abstract of the paper a question is about ("How does X reduce the memory cost ...", "What are the new ...").
GENERIC_WORDS = frozenset(
    [
        "new",
        "recent",
        "main",
        "known",
        "best",
        "better",
        "effective",
        "effectively",
        "use",
        "used",
        "using",
        "uses",
        "make",
        "makes",
        "made",
        "work",
        "works",
        "working",
        "way",
        "ways",
        "approach",
        "approaches",
        "method",
        "methods",
        "technique",
        "techniques",
        "replace",
        "replaces",
        "reduce",
        "reduces",
        "improve",
        "improves",
        "improving",
        "handle",
        "handles",
        "achieve",
        "achieves",
        "compare",
        "compared",
        "comparison",
        "relative",
        "decide",
        "explain",
        "describe",
        "help",
        "helps",
        "allow",
        "allows",
        "enable",
        "enables",
        "provide",
        "provides",
        "cost",
        "costs",
        "without",
        "instead",
        "rather",
        "still",
        "also",
        "much",
        "many",
        "more",
        "less",
        "every",
        "each",
        "some",
        "whether",
        "prove",
        "proved",
        "result",
        "results",
    ]
)


def strip_generic(query: str) -> str:
    return " ".join(w for w in re.findall(r"[\w'-]+", query) if w.lower() not in GENERIC_WORDS)


async def keyword_papers_generic(
    conn: asyncpg.Connection, query: str, filters: S.Filters, k: int, **kw: Any
) -> list[str]:
    """Experiment (--ablations kw-generic): AND query over the question minus GENERIC_WORDS."""
    return await _KW_ORIG(conn, strip_generic(query), filters, k, **kw)


def keyword_papers_gated(models: S.SearchModels, inner: Callable[..., Awaitable[list[str]]] | None = None) -> Any:
    """Experiment (--ablations kw-gated): keyword hits kept only if the vector channel ranks them in its top 200."""

    async def kw(conn: asyncpg.Connection, query: str, filters: S.Filters, k: int, **kwargs: Any) -> list[str]:
        hits = await (inner or _KW_ORIG)(conn, query, filters, k, **kwargs)
        if not hits:
            return hits
        vec = HalfVector((await models.embed_queries([query]))[0])
        top = set(await _VEC_ORIG(conn, vec, filters, 200))
        return [a for a in hits if a in top]

    return kw


async def keyword_papers_or(conn: asyncpg.Connection, query: str, filters: S.Filters, k: int, **_: Any) -> list[str]:
    """Experiment (--ablations kw-or): the keyword channel with OR instead of AND between the question's words,
    ranked by ts_rank_cd, so it returns papers matching most (not all) of the terms."""
    cond, params = filters.sql(3)
    rows = await conn.fetch(
        "SELECT p.arxiv_id FROM papers p, replace(plainto_tsquery('english', $1)::text, ' & ', ' | ') AS qt"
        f" WHERE qt <> '' AND p.abstract_tsv @@ qt::tsquery AND {cond}"
        " ORDER BY ts_rank_cd(p.abstract_tsv, qt::tsquery) DESC LIMIT $2",
        query, k, *params,
    )  # fmt: skip
    return [r["arxiv_id"] for r in rows]


class RankOnlyModels(S.SearchModels):
    """--no-rerank: 'scores' that keep the fused candidate order."""

    async def rerank(self, query: str, passages: list[str]) -> list[float]:
        n = len(passages)
        return [1.0 - i / max(n, 1) for i in range(n)]


# ---- chunk-size ablation -------------------------------------------------------------------------


async def build_chunk_schema(conn: asyncpg.Connection, cfg: AppConfig, embedder: Any, size: int) -> str:
    """Re-chunk every full-text paper from parsed/ at `size` target tokens into eval_cs<size>.chunks."""
    from core.arxiv import file_stem
    from ingest.chunking import chunk_document, embedding_input
    from ingest.pipeline import _count_tokens
    from ingest.tei import parse_tei, read_parsed

    schema = f"eval_cs{size}"
    await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    await conn.execute(f"CREATE TABLE IF NOT EXISTS {schema}.chunks (LIKE public.chunks INCLUDING ALL)")
    ccfg = ChunkingConfig(target_tokens=size, max_tokens=int(size * 1.3), overlap_tokens=min(50, size // 5),
                          min_tokens=min(cfg.chunking.min_tokens, size // 4))  # fmt: skip
    done = {r["arxiv_id"] for r in await conn.fetch(f"SELECT DISTINCT arxiv_id FROM {schema}.chunks")}
    papers = await conn.fetch("SELECT arxiv_id, title FROM papers WHERE state = 'ready' ORDER BY arxiv_id")
    todo = [p for p in papers if p["arxiv_id"] not in done]
    counter = _count_tokens(embedder)
    for n, p in enumerate(todo, 1):
        path = get_settings().data_dir / "parsed" / f"{file_stem(p['arxiv_id'])}.tei.xml.gz"
        if not path.exists():
            continue
        doc = parse_tei(read_parsed(path))
        _, chunks = chunk_document(doc, counter, ccfg)
        if not chunks:
            continue
        vecs = embed_passages(embedder, [embedding_input(p["title"], c) for c in chunks])
        await conn.copy_records_to_table(
            "chunks", schema_name=schema,
            records=[(p["arxiv_id"], c.section, c.position, c.text, c.token_count, c.char_start, c.char_end,
                      HalfVector(v)) for c, v in zip(chunks, vecs, strict=True)],
            columns=["arxiv_id", "section", "position", "text", "token_count", "char_start", "char_end", "embedding"],
        )  # fmt: skip
        if n % 25 == 0:
            print(f"  chunk-size {size}: {n}/{len(todo)} papers re-chunked", flush=True)
    total = await conn.fetchval(f"SELECT count(*) FROM {schema}.chunks")
    avg = await conn.fetchval(f"SELECT avg(token_count) FROM {schema}.chunks")
    print(f"  {schema}.chunks: {total:,} chunks, avg {avg or 0:.0f} tokens (production: {cfg.chunking.target_tokens})")
    return schema


# ---- embedding ablation -----------------------------------------------------------------------------


def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")[:40]


async def build_embedding_schema(conn: asyncpg.Connection, model: Any, model_name: str) -> str:
    """Abstract embeddings for every embedded paper and chunk embeddings for full-text papers, with another model.
    Resumable: rows already present are skipped."""
    schema = f"eval_emb_{_slug(model_name)}"
    dim = model.get_sentence_embedding_dimension()
    await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    await conn.execute(
        f"CREATE TABLE IF NOT EXISTS {schema}.papers_emb (arxiv_id text PRIMARY KEY, emb halfvec({dim}))"
    )
    await conn.execute(
        f"CREATE TABLE IF NOT EXISTS {schema}.chunk_emb (id bigint PRIMARY KEY, arxiv_id text, emb halfvec({dim}))"
    )
    total = await conn.fetchval("SELECT count(*) FROM papers WHERE abstract_embedding IS NOT NULL")
    last = await conn.fetchval(f"SELECT max(arxiv_id) FROM {schema}.papers_emb") or ""
    done = await conn.fetchval(f"SELECT count(*) FROM {schema}.papers_emb")
    started = time.monotonic()
    while True:
        rows = await conn.fetch(
            "SELECT arxiv_id, title, abstract FROM papers WHERE abstract_embedding IS NOT NULL AND arxiv_id > $1"
            " ORDER BY arxiv_id LIMIT 4096",
            last,
        )
        if not rows:
            break
        vecs = await asyncio.to_thread(embed_passages, model, [abstract_text(r["title"], r["abstract"]) for r in rows])
        await conn.copy_records_to_table(
            "papers_emb",
            schema_name=schema,
            records=[(r["arxiv_id"], HalfVector(v)) for r, v in zip(rows, vecs, strict=True)],
        )
        last = rows[-1]["arxiv_id"]
        done += len(rows)
        rate = done / max(time.monotonic() - started, 1)
        print(f"  {schema}: {done:,}/{total:,} abstracts ({rate:.0f}/s)", flush=True)
    await conn.execute(
        f"CREATE INDEX IF NOT EXISTS papers_emb_hnsw ON {schema}.papers_emb USING hnsw (emb halfvec_cosine_ops)"
        " WITH (m = 16, ef_construction = 64)"
    )
    rows = await conn.fetch(
        f"SELECT c.id, c.arxiv_id, c.section, c.text, p.title FROM chunks c JOIN papers p USING (arxiv_id)"
        f" WHERE NOT EXISTS (SELECT 1 FROM {schema}.chunk_emb e WHERE e.id = c.id)"
    )
    from ingest.chunking import Chunk, embedding_input

    for i in range(0, len(rows), 1024):
        batch = rows[i : i + 1024]
        texts = [embedding_input(r["title"], Chunk(section=r["section"] or "", position=0, text=r["text"], token_count=0,
                                                   char_start=0, char_end=0)) for r in batch]  # fmt: skip
        vecs = await asyncio.to_thread(embed_passages, model, texts)
        await conn.copy_records_to_table(
            "chunk_emb", schema_name=schema,
            records=[(r["id"], r["arxiv_id"], HalfVector(v)) for r, v in zip(batch, vecs, strict=True)],
        )  # fmt: skip
    return schema


def embedding_patches(schema: str) -> dict[str, Any]:
    async def vector_papers(conn: asyncpg.Connection, vec: HalfVector, filters: S.Filters, k: int) -> list[str]:
        cond, params = filters.sql(3)
        rows = await conn.fetch(
            f"SELECT arxiv_id FROM (SELECT p.arxiv_id, e.emb <=> $1 AS dist FROM {schema}.papers_emb e"
            f" JOIN papers p USING (arxiv_id) WHERE {cond} ORDER BY e.emb <=> $1 LIMIT $2) s ORDER BY dist",
            vec, k, *params,
        )  # fmt: skip
        return [r["arxiv_id"] for r in rows]

    async def vector_chunks(conn: asyncpg.Connection, vec: HalfVector, papers: list[str], k: int) -> list[int]:
        rows = await conn.fetch(
            f"SELECT id FROM {schema}.chunk_emb WHERE arxiv_id = ANY($3::text[]) ORDER BY emb <=> $1 LIMIT $2",
            vec, k, papers,
        )  # fmt: skip
        return [r["id"] for r in rows]

    anchor_sql = (f"SELECT p.arxiv_id FROM {schema}.papers_emb e JOIN papers p USING (arxiv_id)"
                  f" WHERE {S.ANCHOR_FILTER} AND {{cond}} ORDER BY e.emb <=> $1 LIMIT $2")  # fmt: skip
    return {"vector_papers": vector_papers, "vector_chunks": vector_chunks,
            "anchor_papers": anchor_variant(True, True, anchor_sql)}  # fmt: skip


# ---- retrieval eval ---------------------------------------------------------------------------------


async def retrieval_eval(
    questions: list[dict[str, Any]], cfg: AppConfig, models: S.SearchModels, search_path: str | None = None
) -> dict[str, Any]:
    conn = await raw_connection()
    rows = []
    try:
        if search_path:
            await conn.execute(f"SET search_path = {search_path}, public")
        ready = {r["arxiv_id"] for r in await conn.fetch("SELECT arxiv_id FROM papers WHERE state = 'ready'")}
        await S.search(conn, models, cfg, "warm-up query", with_passages=True)  # GPU warm-up, not timed
        for q in questions:
            t0 = time.perf_counter()
            res = await S.search(conn, models, cfg, q["question"], queries=[q["question"]], with_passages=True)
            ms = (time.perf_counter() - t0) * 1000
            ranked = [p.arxiv_id for p in res.papers]
            passage_papers = [p.arxiv_id for p in res.passages[:K]]
            exp = q["expected_papers"]
            rows.append({
                "id": q["id"], "topic": q["topic"], "mode": q["mode"], "question": q["question"], "expected": exp,
                "field": q["field"], "landmark": q["landmark"], "named": q["named"],
                "recall_at_10": recall_at(exp, ranked), "rr": reciprocal_rank(exp, ranked),
                "passage_hit_at_10": float(bool(set(exp) & set(passage_papers))),
                "expected_full_text": [a for a in exp if a in ready],
                "top10": ranked[:K], "latency_ms": round(ms, 1),
            })  # fmt: skip
    finally:
        await conn.close()
    lat = [r["latency_ms"] for r in rows]
    return {
        "by_tag": by_tag(rows),
        "questions": len(rows),
        "recall_at_10": round(statistics.mean(r["recall_at_10"] for r in rows), 4) if rows else None,
        "mrr": round(statistics.mean(r["rr"] for r in rows), 4) if rows else None,
        "passage_hit_at_10": round(statistics.mean(r["passage_hit_at_10"] for r in rows), 4) if rows else None,
        "latency_ms_p50": percentile(lat, 0.5), "latency_ms_p95": percentile(lat, 0.95),
        "rows": rows,
    }  # fmt: skip


TAGS = ("landmark", "named", "field", "mode")


def by_tag(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """recall@10 and MRR for each value of each tag, e.g. {"landmark=false": {"n": 17, ...}}."""
    out: dict[str, dict[str, Any]] = {}
    for tag in TAGS:
        for value in sorted({str(r[tag]).lower() for r in rows}):
            sel = [r for r in rows if str(r[tag]).lower() == value]
            out[f"{tag}={value}"] = {
                "n": len(sel),
                "recall_at_10": round(statistics.mean(r["recall_at_10"] for r in sel), 4),
                "mrr": round(statistics.mean(r["rr"] for r in sel), 4),
            }
    return out


# ---- answer eval (through the API) ---------------------------------------------------------------------


def _http(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


async def answer_eval(questions: list[dict[str, Any]], sample_n: int, stamp: str) -> dict[str, Any]:
    rows, pairs = [], []
    failed_in_a_row = 0
    for q in questions:
        if failed_in_a_row >= 2:  # e.g. the free-tier daily quota is used up: stop instead of failing every run
            print(f"  stopping: 2 runs failed in a row; not run: {[x['id'] for x in questions[questions.index(q) :]]}")
            break
        created = await asyncio.to_thread(_http, "POST", "/threads", {"question": q["question"], "mode": q["mode"]})
        run_id = created["run_id"]
        deadline = time.monotonic() + 600
        run: dict[str, Any] = {}
        while time.monotonic() < deadline:
            run = await asyncio.to_thread(_http, "GET", f"/runs/{run_id}")
            if run["status"] not in ("queued", "running"):
                break
            await asyncio.sleep(3)
        failed_in_a_row = failed_in_a_row + 1 if run.get("status") == "failed" else 0
        kept = [s for s in run.get("sentences", []) if s["verdict"] != "unsupported_removed"]
        selected = [p["arxiv_id"] for p in run.get("selected_papers", [])]
        cited = sorted({run["sources"][sid]["arxiv_id"] for s in kept for sid in s["sources"] if sid in run["sources"]})
        exp = q["expected_papers"]
        rows.append({
            "id": q["id"], "mode": q["mode"], "run_id": run_id, "status": run.get("status"),
            "duration_s": run.get("duration_s"), "cost_usd": run.get("cost_usd"),
            "sentences": len(run.get("sentences", [])), "removed": run.get("removed_count", 0),
            "written_by": run.get("written_by"), "write_fallback": run.get("write_fallback"),
            "selected_recall_at_10": recall_at(exp, selected), "cited_expected": sorted(set(exp) & set(cited)),
        })  # fmt: skip
        for s in kept:
            for sid in s["sources"]:
                src = run["sources"].get(sid)
                if src:
                    pairs.append({"question_id": q["id"], "run_id": run_id, "sentence": s["text"],
                                  "verdict": s["verdict"], "source_id": sid, "arxiv_id": src["arxiv_id"],
                                  "section": src["section"], "passage": src["text"], "correct": ""})  # fmt: skip
        print(f"  answer #{q['id']:<2} {q['mode']:<5} {run.get('status')} {run.get('duration_s') or 0:6.1f}s "
              f"${run.get('cost_usd') or 0:.4f} removed {run.get('removed_count', 0)}", flush=True)  # fmt: skip
    sample = random.Random(17).sample(pairs, min(sample_n, len(pairs)))
    path = RESULTS / f"citation_sample_{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(pairs[0]) if pairs else ["question_id"])
        w.writeheader()
        w.writerows(sample)
    total_sent = sum(r["sentences"] for r in rows)
    out: dict[str, Any] = {
        "runs": len(rows),
        "removed_claims_rate": round(sum(r["removed"] for r in rows) / total_sent, 4) if total_sent else None,
        "mean_cost_usd": round(statistics.mean(r["cost_usd"] or 0 for r in rows), 5) if rows else None,
        "write_fallbacks": sum(bool(r["write_fallback"]) for r in rows),
        "selected_recall_at_10": round(statistics.mean(r["selected_recall_at_10"] for r in rows), 4) if rows else None,
        "citation_sample": str(path.name), "rows": rows,
    }  # fmt: skip
    for mode in ("quick", "deep"):
        d = [r["duration_s"] for r in rows if r["mode"] == mode and r["duration_s"] is not None]
        out[f"{mode}_latency_s_p50"] = percentile(d, 0.5)
        out[f"{mode}_latency_s_p95"] = percentile(d, 0.95)
        out[f"{mode}_timed_out"] = sum(r["status"] == "timed_out" for r in rows if r["mode"] == mode)
    return out


# ---- report ------------------------------------------------------------------------------------------


def markdown(result: dict[str, Any]) -> str:
    lines = [f"# Eval {result['timestamp']}", "", f"Questions: {result['questions']}", "", "## Retrieval (no LLM calls)", "",
             "| variant | recall@10 | MRR | passage hit@10 | latency p50 | latency p95 |",
             "|---|---|---|---|---|---|"]  # fmt: skip
    for name, r in result["retrieval"].items():
        lines.append(f"| {name} | {r['recall_at_10']:.3f} | {r['mrr']:.3f} | {r['passage_hit_at_10']:.3f} | "
                     f"{r['latency_ms_p50']:.0f} ms | {r['latency_ms_p95']:.0f} ms |")  # fmt: skip
    tags = list(next(iter(result["retrieval"].values()))["by_tag"])
    lines += ["", "Recall@10 / MRR by tag", "", "| variant | " + " | ".join(tags) + " |",
              "|---|" + "---|" * len(tags)]  # fmt: skip
    lines.append(
        "| n | " + " | ".join(str(next(iter(result["retrieval"].values()))["by_tag"][t]["n"]) for t in tags) + " |"
    )
    for name, r in result["retrieval"].items():
        cells = [f"{r['by_tag'][t]['recall_at_10']:.2f} / {r['by_tag'][t]['mrr']:.2f}" for t in tags]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    base = next(iter(result["retrieval"].values()))
    by_topic: dict[str, list[float]] = {}
    for row in base["rows"]:
        by_topic.setdefault(row["topic"], []).append(row["recall_at_10"])
    lines += ["", "Recall@10 by topic (first variant): " + ", ".join(
        f"{t} {statistics.mean(v):.2f} ({len(v)})" for t, v in sorted(by_topic.items()))]  # fmt: skip
    misses = [r for r in base["rows"] if r["recall_at_10"] < 1]
    if misses:
        lines += ["", "Questions below full recall (first variant):", ""]
        lines += [
            f"- #{r['id']} ({r['recall_at_10']:.2f}) {r['question']} expected {', '.join(r['expected'])}"
            for r in misses
        ]
    a = result.get("answers")
    if a:
        lines += ["", "## Answers (pipeline via API, response cache on)", "",
                  "| runs | quick p50 / p95 | deep p50 / p95 | timed out (quick/deep) | cost/run | removed claims | writer fallbacks | selected recall@10 |",
                  "|---|---|---|---|---|---|---|---|",
                  (f"| {a['runs']} | {a['quick_latency_s_p50'] or 0:.1f} / {a['quick_latency_s_p95'] or 0:.1f} s | "
                   f"{a['deep_latency_s_p50'] or 0:.1f} / {a['deep_latency_s_p95'] or 0:.1f} s | "
                   f"{a['quick_timed_out']}/{a['deep_timed_out']} | ${a['mean_cost_usd']:.4f} | "
                   f"{(a['removed_claims_rate'] or 0) * 100:.1f}% | {a['write_fallbacks']} | "
                   f"{a['selected_recall_at_10']:.3f} |"),
                  "", f"Citation sample for manual precision checking: `results/{a['citation_sample']}`"]  # fmt: skip
    return "\n".join(lines) + "\n"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ablations", default="", help="comma list: baseline,no-keyword,no-vector,no-rerank")
    ap.add_argument("--no-keyword", action="store_true")
    ap.add_argument("--no-vector", action="store_true")
    ap.add_argument("--no-rerank", action="store_true")
    ap.add_argument("--embedding", default="")
    ap.add_argument("--chunk-size", type=int)
    ap.add_argument("--answers", action="store_true")
    ap.add_argument("--sample", type=int, default=50)
    args = ap.parse_args()
    setup_logging("WARNING")
    cfg = get_config()
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    questions = load_questions({int(x) for x in args.ids.split(",") if x.strip()} or None, args.limit)

    embedder, reranker = load_embedder(cfg), load_reranker(cfg)
    models = S.SearchModels(embedder, reranker, cfg)
    variants = [v.strip() for v in args.ablations.split(",") if v.strip()]
    if not variants:
        name = "+".join([f for f, on in (("no-keyword", args.no_keyword), ("no-vector", args.no_vector),
                                          ("no-rerank", args.no_rerank)) if on]) or "baseline"  # fmt: skip
        variants = [name]
    retrieval: dict[str, Any] = {}
    for v in variants:
        flags = set(v.split("+"))
        patches: dict[str, Any] = {}
        vcfg, vmodels, search_path = cfg, models, None
        if "no-keyword" in flags:
            patches |= {
                "keyword_papers": _empty,
                "keyword_chunks": _empty,
                "anchor_papers": anchor_variant(True, False),
            }
        if "no-vector" in flags:
            patches |= {"vector_papers": _empty, "vector_chunks": _empty, "anchor_papers": anchor_variant(False, True)}
            # the keyword gate needs the vector channel; without it the gate would drop every keyword hit
            vcfg = vcfg.model_copy(update={"retrieval": vcfg.retrieval.model_copy(update={"keyword_gate_k": 0})})
        if "fts" in flags:  # the old keyword channel as it shipped: Postgres FTS (all terms, ts_rank_cd), generic
            # question words dropped, hits gated by the vector channel's top 200
            vcfg = vcfg.model_copy(update={"retrieval": vcfg.retrieval.model_copy(
                update={"keyword_engine": "fts", "keyword_drop_generic": True, "keyword_gate_k": 200})})  # fmt: skip
        if "drop-generic" in flags:  # drop generic question words from the keyword query
            vcfg = vcfg.model_copy(
                update={"retrieval": vcfg.retrieval.model_copy(update={"keyword_drop_generic": True})}
            )
        if "gate" in flags:  # keyword hits must be in the vector channel's top 200
            vcfg = vcfg.model_copy(update={"retrieval": vcfg.retrieval.model_copy(update={"keyword_gate_k": 200})})
        if "kw-or" in flags:
            patches |= {"keyword_papers": keyword_papers_or}
        if "kw-generic" in flags and "kw-gated" in flags:
            patches |= {"keyword_papers": keyword_papers_gated(models, keyword_papers_generic)}
        elif "kw-generic" in flags:
            patches |= {"keyword_papers": keyword_papers_generic}
        elif "kw-gated" in flags:
            patches |= {"keyword_papers": keyword_papers_gated(models)}
        if "no-rerank" in flags:
            vmodels = RankOnlyModels(embedder, reranker, cfg)
            vcfg = vcfg.model_copy(update={"retrieval": vcfg.retrieval.model_copy(update={"rerank_min_score": None})})
        label = v
        if args.embedding:
            alt = load_embedder(
                cfg.model_copy(update={"models": cfg.models.model_copy(update={"embedding": args.embedding})})
            )
            conn = await raw_connection()
            try:
                schema = await build_embedding_schema(conn, alt, args.embedding)
            finally:
                await conn.close()
            patches |= embedding_patches(schema)
            vmodels = type(vmodels)(alt, reranker, cfg)
            label += f" (embedding {args.embedding})"
        if args.chunk_size:
            conn = await raw_connection()
            try:
                search_path = await build_chunk_schema(conn, cfg, embedder, args.chunk_size)
            finally:
                await conn.close()
            label += f" (chunk size {args.chunk_size})"
        with patched(S, **patches):
            r = await retrieval_eval(questions, vcfg, vmodels, search_path)
        retrieval[label] = r
        print(
            f"{label:<40} recall@10 {r['recall_at_10']:.3f}  MRR {r['mrr']:.3f}  passage hit@10 "
            f"{r['passage_hit_at_10']:.3f}  p50 {r['latency_ms_p50']:.0f} ms  p95 {r['latency_ms_p95']:.0f} ms",
            flush=True,
        )

    result: dict[str, Any] = {"timestamp": stamp, "questions": len(questions), "retrieval": retrieval}
    if args.answers:
        result["answers"] = await answer_eval(questions, args.sample, stamp)
    (RESULTS / f"{stamp}.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    md = markdown(result)
    (RESULTS / f"{stamp}.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    asyncio.run(main())

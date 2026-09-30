"""Hybrid retrieval (spec 11): paper selection, then passage selection within the selected papers.

Papers:   per query keyword top-k + vector top-k over title+abstract (with filters), RRF-fused;
          queries merged by summed RRF; top `rerank_top_n` reranked with the cross-encoder on
          (question, title + ". " + abstract); + capped prior `prior_boost_max * score`.
Passages: for `ready` papers, keyword + vector over their chunks for the question and each query,
          RRF-fused, top `rerank_top_n` reranked, at most `max_passages_per_paper` per paper.
          Selected papers that are not ready contribute their abstract as one passage.
Every stage is timed; timings are returned with the result.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import asyncpg
import numpy as np
import numpy.typing as npt
import orjson
from pgvector import HalfVector

from core.config import AppConfig
from core.embeddings import abstract_text, model_device, rerank

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder, SentenceTransformer


# ---- models ------------------------------------------------------------------------------


class SearchModels:
    """Embedder + reranker. Inference runs in a thread, one call at a time (single GPU)."""

    def __init__(self, embedder: SentenceTransformer, reranker: CrossEncoder, cfg: AppConfig) -> None:
        self.embedder = embedder
        self.reranker = reranker
        self.cfg = cfg
        self._lock = asyncio.Lock()

    @property
    def reranker_device(self) -> str:
        return model_device(self.reranker)

    async def embed_queries(self, queries: Sequence[str]) -> npt.NDArray[np.float32]:
        prefix = self.cfg.models.embedding_query_prefix
        async with self._lock:
            out = await asyncio.to_thread(
                self.embedder.encode, [prefix + q for q in queries], normalize_embeddings=True, convert_to_numpy=True
            )
        return np.asarray(out, dtype=np.float32)

    async def rerank(self, query: str, passages: list[str]) -> list[float]:
        async with self._lock:
            return await asyncio.to_thread(rerank, self.reranker, query, passages)


# ---- data ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    year_from: int | None = None
    year_to: int | None = None
    categories: tuple[str, ...] = ()  # exact ("cs.LG") or archive ("math") matches any category

    def sql(self, first_param: int) -> tuple[str, list[Any]]:
        """SQL condition on table alias `p` and its parameters, numbered from `first_param`."""
        clauses: list[str] = ["p.state <> 'discovered'"]
        params: list[Any] = []
        n = first_param
        if self.year_from is not None:
            clauses.append(f"p.v1_date >= make_date(${n}::int, 1, 1)")
            params.append(self.year_from)
            n += 1
        if self.year_to is not None:
            clauses.append(f"p.v1_date < make_date(${n}::int + 1, 1, 1)")
            params.append(self.year_to)
            n += 1
        if self.categories:
            clauses.append(
                f"EXISTS (SELECT 1 FROM unnest(p.categories) c WHERE c = ANY(${n}::text[])"
                f" OR split_part(c, '.', 1) = ANY(${n}::text[]))"
            )
            params.append(list(self.categories))
        return " AND ".join(clauses), params


@dataclass
class PaperHit:
    arxiv_id: str
    title: str
    abstract: str
    year: int | None
    authors: list[dict[str, str]]
    primary_category: str | None
    badges: dict[str, Any]
    state: str
    prior: float  # papers.score (meta or citation based), 0 if unknown
    rrf: float
    rerank_score: float = 0.0
    final_score: float = 0.0


@dataclass
class Passage:
    arxiv_id: str
    source: str  # chunk | abstract
    chunk_id: int | None
    section: str
    text: str
    char_start: int | None
    char_end: int | None
    rrf: float
    rerank_score: float


@dataclass
class Timings:
    ms: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.ms[name] += (time.perf_counter() - started) * 1000

    def rounded(self) -> dict[str, float]:
        return {k: round(v, 1) for k, v in self.ms.items()}


@dataclass
class SearchResult:
    question: str
    queries: list[str]
    papers: list[PaperHit]
    passages: list[Passage]
    timings_ms: dict[str, float]
    reranker_device: str
    counts: dict[str, int]


def rrf_fuse(rankings: Sequence[Sequence[Any]], k: int) -> list[tuple[Any, float]]:
    """Reciprocal rank fusion: score(d) = sum over lists of 1 / (k + rank), rank starting at 1."""
    scores: dict[Any, float] = defaultdict(float)
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            scores[doc] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0])))


def merge_query_lists(fused_per_query: Sequence[Sequence[tuple[Any, float]]]) -> list[tuple[Any, float]]:
    """Spec 11.2: merge all queries' fused lists by summed RRF score."""
    total: dict[Any, float] = defaultdict(float)
    for fused in fused_per_query:
        for doc, score in fused:
            total[doc] += score
    return sorted(total.items(), key=lambda kv: (-kv[1], str(kv[0])))


def apply_prior(hits: list[PaperHit], prior_boost_max: float, min_score: float | None) -> list[PaperHit]:
    """Spec 11.3: drop below rerank_min_score, add the capped prior, sort by final score."""
    kept = [h for h in hits if min_score is None or h.rerank_score >= min_score]
    for h in kept:
        h.final_score = h.rerank_score + prior_boost_max * min(max(h.prior, 0.0), 1.0)
    return sorted(kept, key=lambda h: -h.final_score)


def cap_per_paper(passages: list[Passage], max_per_paper: int, limit: int) -> list[Passage]:
    """Best-first, at most max_per_paper passages from any one paper, `limit` overall."""
    taken: dict[str, int] = defaultdict(int)
    out = []
    for p in sorted(passages, key=lambda p: -p.rerank_score):
        if taken[p.arxiv_id] < max_per_paper:
            out.append(p)
            taken[p.arxiv_id] += 1
            if len(out) >= limit:
                break
    return out


# ---- SQL -----------------------------------------------------------------------------------


async def _set_vector_params(conn: asyncpg.Connection, cfg: AppConfig) -> None:
    await conn.execute(f"SET hnsw.ef_search = {int(cfg.retrieval.hnsw_ef_search)}")
    await conn.execute("SET hnsw.iterative_scan = relaxed_order")  # spec 10: for filtered queries


async def keyword_papers(conn: asyncpg.Connection, query: str, filters: Filters, k: int) -> list[str]:
    cond, params = filters.sql(3)
    rows = await conn.fetch(
        f"SELECT p.arxiv_id FROM papers p WHERE p.abstract_tsv @@ websearch_to_tsquery('english', $1) AND {cond}"
        " ORDER BY ts_rank_cd(p.abstract_tsv, websearch_to_tsquery('english', $1)) DESC LIMIT $2",
        query, k, *params,
    )  # fmt: skip
    return [r["arxiv_id"] for r in rows]


async def vector_papers(conn: asyncpg.Connection, vec: HalfVector, filters: Filters, k: int) -> list[str]:
    cond, params = filters.sql(3)
    rows = await conn.fetch(
        "SELECT arxiv_id FROM (SELECT p.arxiv_id, p.abstract_embedding <=> $1 AS dist FROM papers p"
        f" WHERE {cond} ORDER BY p.abstract_embedding <=> $1 LIMIT $2) s ORDER BY dist",  # re-sort: relaxed_order
        vec, k, *params,
    )  # fmt: skip
    return [r["arxiv_id"] for r in rows]


async def keyword_chunks(conn: asyncpg.Connection, query: str, papers: list[str], k: int) -> list[int]:
    rows = await conn.fetch(
        "SELECT id FROM chunks WHERE arxiv_id = ANY($3::text[]) AND tsv @@ websearch_to_tsquery('english', $1)"
        " ORDER BY ts_rank_cd(tsv, websearch_to_tsquery('english', $1)) DESC LIMIT $2",
        query, k, papers,
    )  # fmt: skip
    return [r["id"] for r in rows]


async def vector_chunks(conn: asyncpg.Connection, vec: HalfVector, papers: list[str], k: int) -> list[int]:
    rows = await conn.fetch(
        "SELECT id FROM (SELECT id, embedding <=> $1 AS dist FROM chunks WHERE arxiv_id = ANY($3::text[])"
        " ORDER BY embedding <=> $1 LIMIT $2) s ORDER BY dist",
        vec, k, papers,
    )  # fmt: skip
    return [r["id"] for r in rows]


# ---- pipeline ------------------------------------------------------------------------------


async def select_papers(
    conn: asyncpg.Connection,
    models: SearchModels,
    cfg: AppConfig,
    question: str,
    queries: list[str],
    vectors: npt.NDArray[np.float32],
    filters: Filters,
    limit: int,
    timings: Timings,
    counts: dict[str, int],
) -> list[PaperHit]:
    r = cfg.retrieval
    await _set_vector_params(conn, cfg)
    fused_per_query = []
    for q, vec in zip(queries, vectors, strict=True):
        with timings.stage("papers.keyword"):
            kw = await keyword_papers(conn, q, filters, r.paper_keyword_k)
        with timings.stage("papers.vector"):
            vc = await vector_papers(conn, HalfVector(vec), filters, r.paper_vector_k)
        counts["papers.keyword_hits"] += len(kw)
        counts["papers.vector_hits"] += len(vc)
        with timings.stage("papers.fusion"):
            fused_per_query.append(rrf_fuse([kw, vc], r.rrf_k))
    with timings.stage("papers.fusion"):
        candidates = merge_query_lists(fused_per_query)[: r.rerank_top_n]
    counts["papers.candidates"] = len(candidates)
    if not candidates:
        return []

    with timings.stage("papers.load"):
        rows = await conn.fetch(
            "SELECT arxiv_id, title, abstract, extract(year FROM v1_date)::int AS year, authors, primary_category,"
            " badges, state, coalesce(score, meta_score, 0) AS prior FROM papers WHERE arxiv_id = ANY($1::text[])",
            [a for a, _ in candidates],
        )
    by_id = {row["arxiv_id"]: row for row in rows}
    hits = [
        PaperHit(
            arxiv_id=a, title=by_id[a]["title"], abstract=by_id[a]["abstract"], year=by_id[a]["year"],
            authors=_json(by_id[a]["authors"]), primary_category=by_id[a]["primary_category"],
            badges=_json(by_id[a]["badges"]), state=by_id[a]["state"], prior=float(by_id[a]["prior"]), rrf=score,
        )
        for a, score in candidates
        if a in by_id
    ]  # fmt: skip
    with timings.stage("papers.rerank"):
        scores = await models.rerank(question, [abstract_text(h.title, h.abstract) for h in hits])
    for h, s in zip(hits, scores, strict=True):
        h.rerank_score = s
    return apply_prior(hits, r.prior_boost_max, r.rerank_min_score)[:limit]


async def select_passages(
    conn: asyncpg.Connection,
    models: SearchModels,
    cfg: AppConfig,
    question: str,
    queries: list[str],
    vectors: npt.NDArray[np.float32],
    papers: list[PaperHit],
    timings: Timings,
    counts: dict[str, int],
) -> list[Passage]:
    r, a = cfg.retrieval, cfg.agent
    ready = [p.arxiv_id for p in papers if p.state == "ready"]
    passages: list[Passage] = []
    if ready:
        lists: list[list[int]] = []
        for q, vec in zip(queries, vectors, strict=True):
            with timings.stage("passages.keyword"):
                lists.append(await keyword_chunks(conn, q, ready, r.chunk_keyword_k))
            with timings.stage("passages.vector"):
                lists.append(await vector_chunks(conn, HalfVector(vec), ready, r.chunk_vector_k))
        with timings.stage("passages.fusion"):
            fused = rrf_fuse(lists, r.rrf_k)[: r.rerank_top_n]
        counts["passages.candidates"] = len(fused)
        if fused:
            with timings.stage("passages.load"):
                rows = await conn.fetch(
                    "SELECT id, arxiv_id, section, text, char_start, char_end FROM chunks WHERE id = ANY($1::bigint[])",
                    [cid for cid, _ in fused],
                )
            by_id = {row["id"]: row for row in rows}
            chunk_passages = [
                Passage(
                    arxiv_id=c["arxiv_id"],
                    source="chunk",
                    chunk_id=cid,
                    section=c["section"] or "",
                    text=c["text"],
                    char_start=c["char_start"],
                    char_end=c["char_end"],
                    rrf=score,
                    rerank_score=0.0,
                )
                for cid, score in fused
                if (c := by_id.get(cid)) is not None
            ]
            with timings.stage("passages.rerank"):
                scores = await models.rerank(question, [p.text for p in chunk_passages])
            for p, s in zip(chunk_passages, scores, strict=True):
                p.rerank_score = s
            passages = chunk_passages

    # Spec 11.6: selected papers without full text contribute their abstract (always kept).
    abstracts = [
        Passage(arxiv_id=p.arxiv_id, source="abstract", chunk_id=None, section="Abstract", text=p.abstract,
                char_start=None, char_end=None, rrf=p.rrf, rerank_score=p.rerank_score)
        for p in papers
        if p.state != "ready"
    ]  # fmt: skip
    chunks = cap_per_paper(passages, a.max_passages_per_paper, max(0, a.passages_for_answer - len(abstracts)))
    return sorted(chunks + abstracts, key=lambda p: -p.rerank_score)


async def search(
    conn: asyncpg.Connection,
    models: SearchModels,
    cfg: AppConfig,
    question: str,
    queries: list[str] | None = None,
    filters: Filters | None = None,
    limit: int | None = None,
    with_passages: bool = True,
) -> SearchResult:
    """Full section-11 retrieval. `queries` defaults to the question itself (no LLM rewrite)."""
    filters = filters or Filters()
    queries = list(dict.fromkeys(queries or [question]))
    passage_queries = list(dict.fromkeys([question, *queries]))
    timings = Timings()
    counts: dict[str, int] = defaultdict(int)
    with timings.stage("total"):
        with timings.stage("embed_queries"):
            vectors = await models.embed_queries(passage_queries)
        by_query = dict(zip(passage_queries, vectors, strict=True))
        papers = await select_papers(
            conn, models, cfg, question, queries, np.stack([by_query[q] for q in queries]), filters,
            limit or cfg.agent.papers_for_answer, timings, counts,
        )  # fmt: skip
        passages = (
            await select_passages(conn, models, cfg, question, passage_queries, vectors, papers, timings, counts)
            if with_passages else []
        )  # fmt: skip
    return SearchResult(
        question=question, queries=queries, papers=papers, passages=passages, timings_ms=timings.rounded(),
        reranker_device=models.reranker_device, counts=dict(counts),
    )  # fmt: skip


def _json(value: Any) -> Any:
    return orjson.loads(value) if isinstance(value, str | bytes) else value

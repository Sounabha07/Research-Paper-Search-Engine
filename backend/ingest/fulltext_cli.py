"""Full-text commands.

python -m ingest.fulltext_cli landmarks              # queue validated landmarks (fetch:bulk)
python -m ingest.fulltext_cli warm "topic" [--n N]   # paper-level search, queue top N not ready (fetch:bulk)
python -m ingest.fulltext_cli fetch ID [ID ...] [--urgent]
python -m ingest.fulltext_cli recent N               # queue the N most recent tier 1 papers (testing)
python -m ingest.fulltext_cli report                 # fetch/parse timings, chunks, reference resolution
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from collections import Counter
from typing import Any

from core.config import AppConfig, get_config
from core.db import raw_connection
from core.fetchq import enqueue_fulltext
from core.logging import setup_logging
from core.redis import get_redis
from ingest import metrics


def _summary(outcome: dict[str, str]) -> str:
    return ", ".join(f"{k}: {v}" for k, v in sorted(Counter(outcome.values()).items())) or "nothing to do"


async def cmd_landmarks(cfg: AppConfig) -> None:
    conn = await raw_connection()
    try:
        ids = [
            r["arxiv_id"] for r in await conn.fetch("SELECT arxiv_id FROM papers WHERE is_landmark ORDER BY arxiv_id")
        ]
        outcome = await enqueue_fulltext(conn, get_redis(), ids, urgent=False)
    finally:
        await conn.close()
    print(f"landmarks: {len(ids)} -> {_summary(outcome)}")


async def cmd_warm(cfg: AppConfig, topic: str, n: int) -> None:
    from core.embeddings import load_embedder
    from retrieval.papers import search_papers

    embedder = await asyncio.to_thread(load_embedder, cfg)
    conn = await raw_connection()
    try:
        ranked = await search_papers(conn, embedder, cfg, topic)
        ready = {
            r["arxiv_id"]
            for r in await conn.fetch(
                "SELECT arxiv_id FROM papers WHERE arxiv_id = ANY($1) AND state IN ('ready', 'abstract_only')",
                [a for a, _ in ranked],
            )
        }
        picked = [a for a, _ in ranked if a not in ready][:n]
        outcome = await enqueue_fulltext(conn, get_redis(), picked, urgent=False)
        titles = {r["arxiv_id"]: r["title"] for r in await conn.fetch(
            "SELECT arxiv_id, title FROM papers WHERE arxiv_id = ANY($1)", picked)}  # fmt: skip
    finally:
        await conn.close()
    print(f"warm {topic!r}: top {len(picked)} not yet ready -> {_summary(outcome)}")
    for a in picked:
        print(f"  {a:<14} {outcome[a]:<12} {titles.get(a, '')[:90]}")


async def cmd_fetch(ids: list[str], urgent: bool) -> None:
    conn = await raw_connection()
    try:
        outcome = await enqueue_fulltext(conn, get_redis(), ids, urgent=urgent)
    finally:
        await conn.close()
    for a, o in outcome.items():
        print(f"  {a:<14} {o}")


async def cmd_recent(n: int) -> None:
    conn = await raw_connection()
    try:
        ids = [
            r["arxiv_id"]
            for r in await conn.fetch(
                "SELECT arxiv_id FROM papers WHERE state = 'signals_done' AND NOT is_landmark"
                " ORDER BY v1_date DESC NULLS LAST LIMIT $1",
                n,
            )
        ]
        outcome = await enqueue_fulltext(conn, get_redis(), ids, urgent=False)
    finally:
        await conn.close()
    print(f"recent: {len(ids)} -> {_summary(outcome)}")


def _stats(values: list[float], unit: str = "") -> str:
    if not values:
        return "n/a"
    q = statistics.quantiles(values, n=10) if len(values) >= 2 else [values[0]] * 9
    return (
        f"n={len(values)} mean={statistics.mean(values):.2f}{unit} median={statistics.median(values):.2f}{unit} "
        f"p90={q[8]:.2f}{unit} min={min(values):.2f}{unit} max={max(values):.2f}{unit}"
    )


async def cmd_report() -> None:
    conn = await raw_connection()
    redis = get_redis()
    try:
        papers = await conn.fetch(
            "SELECT arxiv_id, state, parser, is_landmark, fail_count, last_error FROM papers"
            " WHERE state IN ('queued_fulltext','pdf_fetched','parsed','embedded','ready','failed','abstract_only')"
            " OR fail_count > 0"
        )
        by_state = Counter(p["state"] for p in papers)
        m: dict[str, dict[str, Any]] = {p["arxiv_id"]: await metrics.load(redis, p["arxiv_id"]) for p in papers}
        ready = [p for p in papers if p["state"] == "ready"]
        chunk_rows = await conn.fetch(
            "SELECT count(*) AS n FROM chunks WHERE arxiv_id = ANY($1) GROUP BY arxiv_id",
            [p["arxiv_id"] for p in ready],
        )
        chunk_counts = [r["n"] for r in chunk_rows]
        refs = await conn.fetchrow(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE r.resolved_id IS NOT NULL AND r.resolved_id NOT LIKE 'stub:%'
                                    AND p.arxiv_id IS NOT NULL) AS to_paper,
                   count(*) FILTER (WHERE r.resolved_id IS NOT NULL AND r.resolved_id NOT LIKE 'stub:%'
                                    AND p.arxiv_id IS NULL) AS to_unknown_arxiv_id,
                   count(*) FILTER (WHERE r.resolved_id LIKE 'stub:%') AS to_stub,
                   count(*) FILTER (WHERE r.resolved_id IS NULL) AS unresolved,
                   count(*) FILTER (WHERE r.title IS NULL AND r.arxiv_id_found IS NULL) AS no_title_no_id
            FROM references_raw r LEFT JOIN papers p ON p.arxiv_id = r.resolved_id
            """
        )
        edges = await conn.fetchval("SELECT count(*) FROM citation_edges")
        stubs = await conn.fetchval("SELECT count(*) FROM stub_papers")
        with_pr = await conn.fetchval("SELECT count(*) FROM papers WHERE pagerank IS NOT NULL")
    finally:
        await conn.close()

    methods: Counter[str] = Counter()
    for v in m.values():
        for k, n in (v.get("resolution") or {}).items():
            if k != "total":
                methods[k] += n
    parsers = Counter(p["parser"] for p in ready)
    print("=== Full-text pipeline report ===")
    print("states:", dict(by_state))
    n_landmarks = sum(1 for p in ready if p["is_landmark"])
    print(f"landmarks ready: {n_landmarks}, others ready: {len(ready) - n_landmarks}")
    print("fetch time per paper (download only):", _stats([v["fetch_s"] for v in m.values() if "fetch_s" in v], "s"))
    print("PDF size (MB):", _stats([v["pdf_bytes"] / 1e6 for v in m.values() if "pdf_bytes" in v]))
    print("GROBID parse time per paper:",
          _stats([v["parse_s"] for v in m.values() if v.get("parser") == "grobid"], "s"))  # fmt: skip
    print(f"parser: grobid {parsers.get('grobid', 0)}, pymupdf fallback {parsers.get('pymupdf', 0)}")
    print("chunks per paper:", _stats([float(c) for c in chunk_counts]))
    total = refs["total"] or 1

    def pct(n: int) -> str:
        return f"{n:,} ({n / total:.1%})"

    print(f"references: {refs['total']:,} total")
    print(f"  resolved to a corpus paper:   {pct(refs['to_paper'])}")
    print(f"  arXiv id not yet in the DB:   {pct(refs['to_unknown_arxiv_id'])}")
    print(f"  stub:                         {pct(refs['to_stub'])}")
    print(f"  unresolved:                   {pct(refs['unresolved'])}"
          f"  ({refs['no_title_no_id']:,} had neither a title nor an arXiv id)")  # fmt: skip
    print("resolution by method:", dict(methods))
    print(f"citation edges {edges:,}, stubs {stubs:,}, papers with PageRank {with_pr:,}")
    failed = [p for p in papers if p["state"] in ("failed", "abstract_only")]
    for p in failed:
        print(f"  {p['state']:<13} {p['arxiv_id']:<14} fails={p['fail_count']} {(p['last_error'] or '')[:120]}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("landmarks")
    w = sub.add_parser("warm")
    w.add_argument("topic")
    w.add_argument("--n", type=int, default=None)
    f = sub.add_parser("fetch")
    f.add_argument("ids", nargs="+")
    f.add_argument("--urgent", action="store_true")
    r = sub.add_parser("recent")
    r.add_argument("n", type=int)
    sub.add_parser("report")
    args = parser.parse_args()

    setup_logging()
    cfg = get_config()
    try:
        if args.cmd == "landmarks":
            await cmd_landmarks(cfg)
        elif args.cmd == "warm":
            await cmd_warm(cfg, args.topic, args.n or cfg.corpus.warm_default_top_n)
        elif args.cmd == "fetch":
            await cmd_fetch(args.ids, args.urgent)
        elif args.cmd == "recent":
            await cmd_recent(args.n)
        else:
            await cmd_report()
    finally:
        await get_redis().aclose()


if __name__ == "__main__":
    asyncio.run(main())

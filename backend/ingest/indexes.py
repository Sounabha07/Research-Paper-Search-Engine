"""Secondary indexes on `papers`, and a bulk mode that drops and rebuilds them.

A non-HOT update or insert must touch every index on the table (two GIN, several btrees,
HNSW). For writes that hit a large share of the table, dropping the secondary indexes
and rebuilding each once afterwards is much faster. Definitions match migration 0001.

Usage: python -m ingest.indexes ensure     # recreate any missing index
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

import asyncpg

from core.logging import setup_logging

log = logging.getLogger(__name__)

PARALLEL_BUILD_WORKERS = 4
# Session-only override for index builds; the server default (1GB) stays as configured.
# The papers HNSW graph outgrows 1GB at ~740k rows.
BUILD_MAINTENANCE_WORK_MEM = "2GB"

PAPER_INDEXES: dict[str, str] = {
    "papers_abstract_embedding_hnsw": (
        "CREATE INDEX IF NOT EXISTS papers_abstract_embedding_hnsw ON papers "
        "USING hnsw (abstract_embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64)"
    ),
    "papers_abstract_tsv_gin": "CREATE INDEX IF NOT EXISTS papers_abstract_tsv_gin ON papers USING gin (abstract_tsv)",
    "papers_title_norm_trgm": (
        "CREATE INDEX IF NOT EXISTS papers_title_norm_trgm ON papers USING gin (title_norm gin_trgm_ops)"
    ),
    "papers_title_norm": "CREATE INDEX IF NOT EXISTS papers_title_norm ON papers (title_norm)",
    "papers_primary_category_v1_date": (
        "CREATE INDEX IF NOT EXISTS papers_primary_category_v1_date ON papers (primary_category, v1_date)"
    ),
    "papers_score": "CREATE INDEX IF NOT EXISTS papers_score ON papers (score)",
    "papers_state": "CREATE INDEX IF NOT EXISTS papers_state ON papers (state)",
}
HNSW_INDEX = "papers_abstract_embedding_hnsw"


async def existing(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT indexname FROM pg_indexes WHERE tablename = 'papers'")
    return {r["indexname"] for r in rows}


async def drop_secondary(conn: asyncpg.Connection, keep: frozenset[str] = frozenset()) -> list[str]:
    dropped = [name for name in PAPER_INDEXES if name not in keep and name in await existing(conn)]
    for name in dropped:
        await conn.execute(f"DROP INDEX IF EXISTS {name}")
    log.info("dropped papers indexes for bulk write", extra={"indexes": dropped})
    return dropped


async def ensure(conn: asyncpg.Connection) -> dict[str, float]:
    """Create any missing index. Returns build seconds per index created."""
    await conn.execute(f"SET max_parallel_maintenance_workers = {PARALLEL_BUILD_WORKERS}")
    await conn.execute(f"SET maintenance_work_mem = '{BUILD_MAINTENANCE_WORK_MEM}'")
    have = await existing(conn)
    built: dict[str, float] = {}
    for name, ddl in PAPER_INDEXES.items():
        if name in have:
            continue
        started = time.monotonic()
        await conn.execute(ddl)
        built[name] = round(time.monotonic() - started, 1)
        log.info("built papers index", extra={"index": name, "s": built[name]})
    await conn.execute("RESET maintenance_work_mem")
    return built


async def _cli() -> int:
    from core.db import raw_connection

    setup_logging()
    if sys.argv[1:] != ["ensure"]:
        print("usage: python -m ingest.indexes ensure", file=sys.stderr)
        return 2
    conn = await raw_connection()
    try:
        built = await ensure(conn)
    finally:
        await conn.close()
    print(f"indexes built: {built}" if built else "all papers indexes present")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_cli()))

"""Compact catalog of every arXiv record in the Kaggle snapshot (id, title, title_norm, year, category).

Reference resolution looks cited arXiv papers up here before calling the arXiv API, which is then only
needed for ids newer than the snapshot. The metadata pass fills it while streaming; this module can also
reload it on its own:

Usage: python -m ingest.catalog [--skip-download]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from typing import Any

import asyncpg

from core.config import get_settings
from core.db import raw_connection
from core.logging import setup_logging
from ingest import kaggle
from ingest.text import collapse_ws, normalize_title

log = logging.getLogger(__name__)

BATCH = 50_000
COLUMNS = ["arxiv_id", "title", "title_norm", "year", "primary_category"]
# The table is replaced on every load; secondary indexes are rebuilt once at the end.
_INDEXES = {
    "arxiv_catalog_title_norm": "CREATE INDEX arxiv_catalog_title_norm ON arxiv_catalog (title_norm)",
    "arxiv_catalog_title_norm_trgm": (
        "CREATE INDEX arxiv_catalog_title_norm_trgm ON arxiv_catalog USING gin (title_norm gin_trgm_ops)"
    ),
}


class CatalogLoader:
    """Streams into an unconstrained staging table (the Kaggle file repeats some ids), then keeps
    one row per id."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn
        self.rows: list[tuple[Any, ...]] = []
        self.count = 0
        self.unique = 0

    async def start(self) -> None:
        await self.conn.execute("DROP TABLE IF EXISTS arxiv_catalog_load")
        await self.conn.execute(
            "CREATE UNLOGGED TABLE arxiv_catalog_load"
            " (arxiv_id text, title text, title_norm text, year smallint, primary_category text)"
        )

    async def add(self, arxiv_id: str, title: str, year: int | None, primary_category: str | None) -> None:
        title = collapse_ws(title)
        norm = normalize_title(title)
        if not title or not norm:
            return
        self.rows.append((arxiv_id, title, norm, year, primary_category))
        if len(self.rows) >= BATCH:
            await self.flush()

    async def flush(self) -> None:
        if self.rows:
            await self.conn.copy_records_to_table("arxiv_catalog_load", records=self.rows, columns=COLUMNS)
            self.count += len(self.rows)
            self.rows.clear()

    async def finish(self) -> float:
        await self.flush()
        started = time.monotonic()
        for name in _INDEXES:
            await self.conn.execute(f"DROP INDEX IF EXISTS {name}")
        async with self.conn.transaction():
            await self.conn.execute("TRUNCATE arxiv_catalog")
            status = await self.conn.execute(
                "INSERT INTO arxiv_catalog SELECT DISTINCT ON (arxiv_id) arxiv_id, title, title_norm, year,"
                " primary_category FROM arxiv_catalog_load ORDER BY arxiv_id, year DESC NULLS LAST"
            )
        self.unique = int(status.split()[-1])
        await self.conn.execute("DROP TABLE arxiv_catalog_load")
        await self.conn.execute("SET maintenance_work_mem = '1GB'")
        for ddl in _INDEXES.values():
            await self.conn.execute(ddl)
        await self.conn.execute("RESET maintenance_work_mem")
        await self.conn.execute("ANALYZE arxiv_catalog")
        return time.monotonic() - started


async def load_catalog_only(skip_download: bool) -> str:
    from ingest.metadata import parse_v1_date

    raw_dir = get_settings().data_dir / "raw"
    zip_path = raw_dir / kaggle.ZIP_NAME
    if not skip_download:
        zip_path = kaggle.download(raw_dir)
    elif not kaggle.is_valid_zip(zip_path):
        raise SystemExit(f"No valid {zip_path}; run without --skip-download")
    started = time.monotonic()
    conn = await raw_connection()
    try:
        loader = CatalogLoader(conn)
        await loader.start()
        for rec in kaggle.iter_records(zip_path):
            if rec and rec.get("id") and rec.get("title"):
                v1 = parse_v1_date(rec)
                cats = (rec.get("categories") or "").split()
                await loader.add(str(rec["id"]), rec["title"], v1.year if v1 else None, cats[0] if cats else None)
        index_s = await loader.finish()
        size = await conn.fetchval("SELECT pg_size_pretty(pg_total_relation_size('arxiv_catalog'))")
    finally:
        await conn.close()
    return (
        f"=== arXiv catalog ===\n{loader.count:,} records, {loader.unique:,} unique ids "
        f"({loader.count - loader.unique:,} duplicates) in {time.monotonic() - started:.0f}s "
        f"(indexes {index_s:.0f}s); table + indexes {size}"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-download", action="store_true")
    args = parser.parse_args()
    setup_logging()
    print(await load_catalog_only(args.skip_download), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

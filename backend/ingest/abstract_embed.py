"""Abstract embeddings for tier 1 papers (spec 8), plus the paper-level HNSW index.

Usage: python -m ingest.abstract_embed

Idempotent: only rows with abstract_embedding IS NULL are embedded. For a large backlog
the HNSW index is dropped first and rebuilt once at the end, which is far faster than
maintaining it row by row.
"""

from __future__ import annotations

import asyncio
import logging
import time

from pgvector import HalfVector

from core.config import AppConfig, get_config
from core.db import raw_connection
from core.embeddings import abstract_text, embed_passages, load_embedder
from core.logging import setup_logging
from ingest import indexes

log = logging.getLogger(__name__)

FETCH_SIZE = 8192
REBUILD_THRESHOLD = 50_000  # above this backlog, drop and rebuild the papers indexes

_STAGE = "CREATE TEMP TABLE IF NOT EXISTS emb_stage (arxiv_id text, emb halfvec(384)) ON COMMIT DELETE ROWS"
_APPLY = "UPDATE papers p SET abstract_embedding = s.emb FROM emb_stage s WHERE p.arxiv_id = s.arxiv_id"
_NEXT = """
SELECT arxiv_id, title, abstract FROM papers
WHERE abstract_embedding IS NULL AND state <> 'discovered' AND arxiv_id > $1
ORDER BY arxiv_id LIMIT $2
"""


async def embed_abstracts(cfg: AppConfig) -> str:
    conn = await raw_connection()
    rebuilt: dict[str, float] = {}
    try:
        pending = await conn.fetchval(
            "SELECT count(*) FROM papers WHERE abstract_embedding IS NULL AND state <> 'discovered'"
        )
        total = await conn.fetchval("SELECT count(*) FROM papers")
        print(f"abstract embeddings: {pending:,} pending of {total:,} papers", flush=True)
        if pending >= REBUILD_THRESHOLD:
            await indexes.drop_secondary(conn)

        done = 0
        if pending:
            model = load_embedder(cfg)
            await conn.execute(_STAGE)
            started = time.monotonic()
            last_id = ""
            while True:
                rows = await conn.fetch(_NEXT, last_id, FETCH_SIZE)
                if not rows:
                    break
                last_id = rows[-1]["arxiv_id"]
                vectors = embed_passages(model, [abstract_text(r["title"], r["abstract"]) for r in rows])
                records = [(r["arxiv_id"], HalfVector(v)) for r, v in zip(rows, vectors, strict=True)]
                async with conn.transaction():
                    await conn.copy_records_to_table("emb_stage", records=records, columns=["arxiv_id", "emb"])
                    await conn.execute(_APPLY)
                done += len(rows)
                rate = done / max(time.monotonic() - started, 1e-6)
                log.info(
                    "abstract embedding progress",
                    extra={
                        "done": done,
                        "pending": pending,
                        "per_s": round(rate),
                        "eta_s": int((pending - done) / rate),
                    },
                )

    finally:
        print("ensuring papers indexes (the HNSW build can take several minutes) ...", flush=True)
        rebuilt = await indexes.ensure(conn)
        if rebuilt:
            await conn.execute("VACUUM (ANALYZE) papers")
        embedded = await conn.fetchval("SELECT count(*) FROM papers WHERE abstract_embedding IS NOT NULL")
        await conn.close()
    msg = f"=== Abstract embeddings ===\nembedded this run {done:,}; total with embedding {embedded:,} of {total:,}"
    if rebuilt:
        msg += f"\nrebuilt indexes (s): {rebuilt}"
    return msg


async def main() -> None:
    setup_logging()
    print(await embed_abstracts(get_config()), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

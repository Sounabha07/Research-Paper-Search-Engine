"""Nightly job (spec 16): OAI-PMH harvest for cs and math, then abstract embeddings, foundational papers,
citation scores and PageRank from the current edges.

The harvest itself runs in the fetcher (all arXiv traffic does); it enqueues post_harvest_job when done.
`python -m ingest.nightly` queues a run now (make nightly).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import cast

import asyncpg
from redis.asyncio import Redis

from core.config import AppConfig, get_config
from core.db import raw_connection
from core.fetchq import oai_job
from core.logging import setup_logging
from core.redis import FETCH_BULK, OAI_LAST_HARVEST, get_redis
from ingest.metadata import window_cutoff

log = logging.getLogger(__name__)

SETS = ["cs", "math"]
FIRST_RUN_DAYS = 3  # no previous harvest recorded: look back this far (the Kaggle snapshot covers the rest)


def harvest_from(last: str | None, today: date | None = None) -> str:
    """OAI `from` date: one day before the last harvest (datestamps are days, overlap is harmless: upserts)."""
    today = today or datetime.now(UTC).date()
    if last:
        return (date.fromisoformat(last) - timedelta(days=1)).isoformat()
    return (today - timedelta(days=FIRST_RUN_DAYS)).isoformat()


async def queue_harvest(redis: Redis) -> str:
    last = await redis.get(OAI_LAST_HARVEST)
    frm = harvest_from(last.decode() if isinstance(last, bytes) else last)
    # front of the bulk queue: ahead of warm/landmark fetches, still behind urgent ones
    await cast(asyncio.Future[int], redis.lpush(FETCH_BULK, oai_job(SETS, frm)))
    log.info("nightly harvest queued", extra={"sets": SETS, "from": frm})
    return frm


async def update_foundational(conn: asyncpg.Connection, cfg: AppConfig) -> dict[str, int]:
    """Foundational = cs/math papers older than the window, cited by at least foundational_min_fulltext_citations
    full-text papers (spec 7). New ones only known from reference lookups ('discovered') join the pipeline."""
    cutoff = window_cutoff(cfg.corpus.years_back)
    n = cfg.corpus.foundational_min_fulltext_citations
    patterns = [f"{p}%" for p in cfg.corpus.categories_prefixes]
    status = await conn.execute(
        "UPDATE papers p SET is_foundational = x.want FROM ("
        " SELECT arxiv_id, (v1_date < $1 AND fulltext_citations >= $2"
        "  AND EXISTS (SELECT 1 FROM unnest(categories) c WHERE c LIKE ANY($3::text[]))) AS want"
        " FROM papers WHERE is_foundational OR fulltext_citations >= $2) x"
        " WHERE p.arxiv_id = x.arxiv_id AND p.is_foundational IS DISTINCT FROM x.want",
        cutoff, n, patterns,
    )  # fmt: skip
    promoted = await conn.execute(
        "UPDATE papers SET state = 'metadata_done' WHERE is_foundational AND state = 'discovered'"
    )
    total = await conn.fetchval("SELECT count(*) FROM papers WHERE is_foundational")
    return {"changed": int(status.split()[-1]), "promoted": int(promoted.split()[-1]), "foundational": int(total)}


async def post_harvest(cfg: AppConfig) -> str:
    from ingest.abstract_embed import embed_abstracts
    from ingest.scores import recompute_scores

    conn = await raw_connection()
    try:
        found = await update_foundational(conn, cfg)
    finally:
        await conn.close()
    emb = await embed_abstracts(cfg)
    scores = await recompute_scores(cfg)
    log.info("nightly post-harvest done", extra=found)
    return f"foundational {found}\n{emb}\n{scores}"


async def main() -> None:
    setup_logging()
    get_config()
    redis = get_redis()
    try:
        print(f"queued OAI-PMH harvest of {SETS} from {await queue_harvest(redis)}", flush=True)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())

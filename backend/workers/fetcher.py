"""The single process that talks to arXiv (spec 6.4). Plain asyncio loop, not arq.

Pops `BLPOP fetch:urgent fetch:bulk` (urgent always drains first), enforces
`arxiv.min_interval_s` between any two requests with one in-process limiter, writes PDFs
to `pdf_tmp/`, and hands them to the arq pipeline. Also fills metadata for referenced
arXiv ids missing from the DB (spec 6.2b).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import time
from collections.abc import Awaitable
from typing import Any, cast

import asyncpg
import orjson
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from core.arxiv import ArxivClient, AtomEntry, FetchError, RateLimiter, file_stem
from core.config import AppConfig, Settings, ensure_data_dirs, get_config, get_settings, require_env
from core.fetchq import parse_job, pdf_job
from core.logging import setup_logging
from core.redis import FETCH_BULK, FETCH_URGENT, FETCHER_HEARTBEAT, fetch_done_channel, get_redis
from ingest import metrics
from ingest.references import lock_citation_graph, resolve_new_papers
from ingest.text import normalize_title

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 30
POP_TIMEOUT_S = 5
MAX_FETCH_FAILURES = 3


class Fetcher:
    def __init__(self, cfg: AppConfig, settings: Settings, pool: asyncpg.Pool, arq: ArqRedis) -> None:
        self.cfg = cfg
        self.settings = settings
        self.pool = pool
        self.arq = arq
        self.redis = get_redis()
        self.client = ArxivClient(cfg, RateLimiter(cfg.arxiv.min_interval_s))

    async def recover(self) -> None:
        """Re-queue papers left in queued_fulltext whose job was lost (e.g. crash mid-fetch)."""
        queued: set[str] = set()
        for q in (FETCH_URGENT, FETCH_BULK):
            for raw in await cast(Awaitable[list[str]], self.redis.lrange(q, 0, -1)):
                job = json.loads(raw)
                if job.get("type") == "pdf":
                    queued.add(job["arxiv_id"])
        stuck = [
            r["arxiv_id"] for r in await self.pool.fetch("SELECT arxiv_id FROM papers WHERE state = 'queued_fulltext'")
        ]
        lost = [i for i in stuck if i not in queued]
        if lost:
            await cast(Awaitable[int], self.redis.rpush(FETCH_BULK, *(pdf_job(i) for i in lost)))
            log.warning("re-queued lost fetch jobs", extra={"count": len(lost)})

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            item = await cast(
                Awaitable[list[str] | None], self.redis.blpop([FETCH_URGENT, FETCH_BULK], timeout=POP_TIMEOUT_S)
            )
            if item is None:
                continue
            queue, raw = item
            try:
                job = parse_job(raw)
                if job["type"] == "pdf":
                    await self.fetch_pdf(job["arxiv_id"], urgent=queue == FETCH_URGENT)
                else:
                    await self.fetch_meta(job["ids"])
            except Exception:
                log.exception("fetch job crashed", extra={"job": raw})

    async def fetch_pdf(self, arxiv_id: str, urgent: bool) -> None:
        row = await self.pool.fetchrow("SELECT state, fail_count FROM papers WHERE arxiv_id = $1", arxiv_id)
        if row is None or row["state"] != "queued_fulltext":
            return  # duplicate job (urgent re-push) or already handled
        waited_s = None
        started = time.monotonic()
        try:
            result = await self.client.fetch_pdf(arxiv_id)
        except FetchError as e:
            fails = row["fail_count"] + 1
            final = e.permanent or fails >= MAX_FETCH_FAILURES
            state = "abstract_only" if final else "failed"
            await self.pool.execute(
                "UPDATE papers SET state = $2, fail_count = $3, last_error = $4 WHERE arxiv_id = $1",
                arxiv_id, state, fails, f"fetch: {e}"[:1000],
            )  # fmt: skip
            log.warning(
                "pdf fetch failed", extra={"arxiv_id": arxiv_id, "fails": fails, "state": state, "error": str(e)}
            )
            if final:
                await self.redis.publish(fetch_done_channel(arxiv_id), "abstract_only")
            else:  # retry later, behind everything already queued
                await self.pool.execute("UPDATE papers SET state = 'queued_fulltext' WHERE arxiv_id = $1", arxiv_id)
                await cast(Awaitable[int], self.redis.rpush(FETCH_BULK, pdf_job(arxiv_id)))
            return
        waited_s = time.monotonic() - started - result.download_s

        path = self.settings.data_dir / "pdf_tmp" / f"{file_stem(arxiv_id)}.pdf"
        tmp = path.with_suffix(".part")
        tmp.write_bytes(result.content)
        tmp.replace(path)
        await self.pool.execute("UPDATE papers SET state = 'pdf_fetched' WHERE arxiv_id = $1", arxiv_id)
        await metrics.record(
            self.redis, arxiv_id, fetch_s=round(result.download_s, 2), fetch_wait_s=round(waited_s, 2),
            pdf_bytes=len(result.content), urgent=urgent,
        )  # fmt: skip
        await self.arq.enqueue_job("parse_paper", arxiv_id, _job_id=f"parse:{arxiv_id}")
        log.info(
            "pdf fetched",
            extra={
                "arxiv_id": arxiv_id,
                "bytes": len(result.content),
                "s": round(result.download_s, 2),
                "urgent": urgent,
            },
        )

    async def fetch_meta(self, ids: list[str]) -> None:
        known = {
            r["arxiv_id"] for r in await self.pool.fetch("SELECT arxiv_id FROM papers WHERE arxiv_id = ANY($1)", ids)
        }
        missing = [i for i in ids if i not in known]
        if not missing:
            return
        try:
            entries = await self.client.query_ids(missing)
        except FetchError as e:
            log.warning("arxiv metadata lookup failed", extra={"ids": len(missing), "error": str(e)})
            return
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.executemany(
                "INSERT INTO papers (arxiv_id, title, title_norm, authors, abstract, categories, primary_category,"
                " v1_date, journal_ref, comments, state) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'discovered')"
                " ON CONFLICT (arxiv_id) DO NOTHING",
                [_meta_row(e) for e in entries],
            )
            await lock_citation_graph(conn)
            matched = await resolve_new_papers(conn, [e.arxiv_id for e in entries])
        log.info(
            "arxiv metadata filled",
            extra={"requested": len(missing), "found": len(entries), "title_matches": matched},
        )

    async def aclose(self) -> None:
        await self.client.aclose()


def _meta_row(e: AtomEntry) -> tuple[Any, ...]:
    return (
        e.arxiv_id, e.title, normalize_title(e.title), orjson.dumps(e.authors).decode(), e.abstract, e.categories,
        e.primary_category, e.published, e.journal_ref, e.comments,
    )  # fmt: skip


async def heartbeat(stop: asyncio.Event) -> None:
    redis = get_redis()
    while not stop.is_set():
        await redis.set(FETCHER_HEARTBEAT, str(time.time()), ex=HEARTBEAT_INTERVAL_S * 3)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), HEARTBEAT_INTERVAL_S)


async def main() -> None:
    setup_logging()
    cfg = get_config()
    settings = get_settings()
    ensure_data_dirs()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows dev runs
            loop.add_signal_handler(sig, stop.set)

    pool = await asyncpg.create_pool(
        host=settings.postgres_host, port=settings.postgres_port, user=settings.postgres_user,
        password=require_env("POSTGRES_PASSWORD"), database=settings.postgres_db, min_size=1, max_size=3,
    )  # fmt: skip
    arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    fetcher = Fetcher(cfg, settings, pool, arq)
    await fetcher.recover()
    log.info("fetcher started", extra={"min_interval_s": cfg.arxiv.min_interval_s})
    try:
        await asyncio.gather(heartbeat(stop), fetcher.run(stop))
    finally:
        await fetcher.aclose()
        await arq.aclose()
        await pool.close()
        await get_redis().aclose()
        log.info("fetcher stopped")


if __name__ == "__main__":
    asyncio.run(main())

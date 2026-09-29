"""arq worker entrypoint: `arq workers.arq_worker.WorkerSettings`.

Full-text pipeline stages (spec 8), the pdf_tmp janitor, and batch jobs. On startup it
re-enqueues papers left mid-pipeline, so restarts never lose or duplicate work.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar

import asyncpg
from arq import Retry, cron
from arq.connections import ArqRedis, RedisSettings
from arq.worker import func
from pgvector.asyncpg import register_vector

from core.config import ensure_data_dirs, get_config, get_settings, require_env
from core.embeddings import load_embedder
from core.logging import setup_logging
from core.redis import ARQ_QUEUE, get_redis
from ingest.grobid import GrobidClient
from ingest.janitor import run_janitor
from ingest.pipeline import MAX_PARSE_FAILURES, PipelineDeps, chunk_embed_stage, parse_stage, pdf_path

log = logging.getLogger(__name__)

PARSE_RETRY_DEFER_S = 60


async def ping(ctx: dict[str, Any]) -> str:
    return "pong"


async def parse_paper(ctx: dict[str, Any], arxiv_id: str) -> str:
    deps: PipelineDeps = ctx["deps"]
    try:
        outcome = await parse_stage(deps, arxiv_id)
    except RuntimeError as e:
        log.warning("parse failed, will retry", extra={"arxiv_id": arxiv_id, "error": str(e)[:300]})
        raise Retry(defer=PARSE_RETRY_DEFER_S) from e
    if outcome in ("grobid", "pymupdf"):
        await ctx["redis"].enqueue_job("chunk_paper", arxiv_id, _job_id=f"chunk:{arxiv_id}")
    return outcome


# Transient conflicts between concurrent transactions: retry instead of failing the job.
TRANSIENT_DB_ERRORS = (asyncpg.DeadlockDetectedError, asyncpg.SerializationError)


async def chunk_paper(ctx: dict[str, Any], arxiv_id: str) -> str:
    try:
        return await chunk_embed_stage(ctx["deps"], arxiv_id)
    except TRANSIENT_DB_ERRORS as e:
        log.warning("transient db conflict, retrying", extra={"arxiv_id": arxiv_id, "error": type(e).__name__})
        raise Retry(defer=5) from e


async def janitor_job(ctx: dict[str, Any]) -> str:
    deps: PipelineDeps = ctx["deps"]
    async with deps.pool.acquire() as conn:
        res = await run_janitor(conn, deps.settings.data_dir / "pdf_tmp")
    return str(res)


async def embed_abstracts_job(ctx: dict[str, Any]) -> str:
    from ingest.abstract_embed import embed_abstracts

    return await embed_abstracts(ctx["cfg"])


async def recompute_scores_job(ctx: dict[str, Any]) -> str:
    from ingest.scores import recompute_scores

    return await recompute_scores(ctx["cfg"])


async def recover(redis: ArqRedis, deps: PipelineDeps) -> None:
    rows = await deps.pool.fetch(
        "SELECT arxiv_id, state FROM papers WHERE state IN ('pdf_fetched', 'parsed', 'embedded')"
    )
    n = 0
    for r in rows:
        if r["state"] == "pdf_fetched":
            if pdf_path(deps.settings, r["arxiv_id"]).exists():
                n += bool(await redis.enqueue_job("parse_paper", r["arxiv_id"], _job_id=f"parse:{r['arxiv_id']}"))
        else:
            n += bool(await redis.enqueue_job("chunk_paper", r["arxiv_id"], _job_id=f"chunk:{r['arxiv_id']}"))
    if n:
        log.info("re-enqueued in-flight papers", extra={"count": n})


async def startup(ctx: dict[str, Any]) -> None:
    setup_logging()
    cfg = get_config()
    settings = get_settings()
    ensure_data_dirs()

    async def _init(conn: asyncpg.Connection) -> None:
        await register_vector(conn)

    pool = await asyncpg.create_pool(
        host=settings.postgres_host, port=settings.postgres_port, user=settings.postgres_user,
        password=require_env("POSTGRES_PASSWORD"), database=settings.postgres_db, min_size=1, max_size=6, init=_init,
    )  # fmt: skip
    embedder = await asyncio.to_thread(load_embedder, cfg)
    ctx["cfg"] = cfg
    ctx["deps"] = PipelineDeps(
        cfg=cfg,
        settings=settings,
        pool=pool,
        redis=get_redis(),
        grobid=GrobidClient(settings.grobid_url),
        embedder=embedder,
        gpu_lock=asyncio.Lock(),
    )
    await recover(ctx["redis"], ctx["deps"])
    log.info("worker started")


async def shutdown(ctx: dict[str, Any]) -> None:
    deps: PipelineDeps | None = ctx.get("deps")
    if deps is not None:
        await deps.grobid.aclose()
        await deps.pool.close()


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        ping,
        func(parse_paper, max_tries=MAX_PARSE_FAILURES + 1, timeout=900, keep_result=0),
        func(chunk_paper, max_tries=5, timeout=900, keep_result=0),
        func(embed_abstracts_job, timeout=6 * 3600, max_tries=1),
        func(recompute_scores_job, timeout=3600, max_tries=1),
    ]
    cron_jobs: ClassVar[list[Any]] = [cron(janitor_job, minute={0, 30}, run_at_startup=True)]
    on_startup = startup
    on_shutdown = shutdown
    queue_name = ARQ_QUEUE
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    health_check_interval = 30
    max_jobs = 8

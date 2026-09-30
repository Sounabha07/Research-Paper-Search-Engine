"""arq worker entrypoint: `arq workers.arq_worker.WorkerSettings`.

Full-text pipeline stages (spec 8), the pdf_tmp janitor, and batch jobs. On startup it
re-enqueues papers left mid-pipeline, so restarts never lose or duplicate work.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, ClassVar

import asyncpg
from arq import Retry, cron
from arq.connections import ArqRedis, RedisSettings
from arq.worker import func
from pgvector.asyncpg import register_vector

from agent.run import AgentDeps, execute_run
from core.config import ensure_data_dirs, get_config, get_settings, require_env
from core.db import get_sessionmaker
from core.embeddings import load_embedder, load_reranker
from core.llm import LLMClient, db_call_logger
from core.logging import setup_logging
from core.redis import ARQ_QUEUE, get_redis
from ingest.grobid import GrobidClient
from ingest.janitor import run_janitor
from ingest.pipeline import (
    MAX_PARSE_FAILURES,
    REFS_PENDING,
    PipelineDeps,
    chunk_embed_stage,
    parse_stage,
    pdf_path,
    resolve_references,
)
from retrieval.search import Filters, SearchModels, result_to_api, search

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


async def resolve_refs_job(ctx: dict[str, Any], arxiv_id: str) -> str:
    """References of a ready paper left unresolved by a restart."""
    stats = await resolve_references(ctx["deps"], arxiv_id)
    return f"{stats.resolved}/{stats.total} refs resolved"


async def search_job(ctx: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """GET /search runs here because the models live only in the worker."""
    deps: PipelineDeps = ctx["deps"]
    filters = Filters(
        year_from=params.get("year_from"),
        year_to=params.get("year_to"),
        categories=tuple(params.get("categories") or ()),
    )
    async with deps.pool.acquire() as conn:
        result = await search(
            conn, ctx["search_models"], ctx["cfg"], params["q"], filters=filters, limit=params.get("limit"),
            with_passages=params.get("passages", True),
        )  # fmt: skip
    return result_to_api(result)


async def research_run_job(ctx: dict[str, Any], run_id: str) -> str:
    """Spec 12: one research run (quick mode in milestone 5)."""
    return await execute_run(ctx["agent"], uuid.UUID(run_id))


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


async def nightly_job(ctx: dict[str, Any]) -> str:
    """Spec 16, 02:00 UTC: queue the OAI-PMH harvest in the fetcher (it enqueues post_harvest_job when done).
    Disabled by config: arXiv refuses ListRecords; the weekly snapshot job brings new papers instead."""
    from ingest.nightly import queue_harvest

    frm = await queue_harvest(get_redis())
    return f"harvest queued from {frm}" if frm else "OAI-PMH harvest disabled"


async def weekly_snapshot_job(ctx: dict[str, Any]) -> str:
    """Mondays 00:30 UTC: latest Kaggle snapshot, new/changed papers only, then the post-harvest steps."""
    from ingest.weekly import weekly_snapshot

    return await weekly_snapshot(ctx["cfg"])


async def post_harvest_job(ctx: dict[str, Any]) -> str:
    from ingest.nightly import post_harvest

    return await post_harvest(ctx["cfg"])


async def digest_job(ctx: dict[str, Any], feed_id: str | None = None, days: int | None = None) -> Any:
    """Spec 16, weekly (Monday 03:00 UTC): a digest for every feed. With feed_id: that feed now (API)."""
    from agent.digest import build_all, build_digest

    agent, models = ctx["agent"], ctx["search_models"]
    if feed_id is None:
        return await build_all(agent.pool, agent.llm, models, agent.cfg)
    feed = await agent.pool.fetchrow("SELECT * FROM feeds WHERE id = $1", uuid.UUID(feed_id))
    if feed is None:
        return None
    return await build_digest(agent.pool, agent.llm, models, agent.cfg, dict(feed), days=days)


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
    for arxiv_id in await redis.smembers(REFS_PENDING):  # type: ignore[misc]
        a = arxiv_id.decode() if isinstance(arxiv_id, bytes) else arxiv_id
        n += bool(await redis.enqueue_job("resolve_refs_job", a, _job_id=f"refs:{a}"))
    if n:
        log.info("re-enqueued in-flight papers", extra={"count": n})
    # Spec 12: runs left 'running' by a restart resume from their last completed step; queued runs whose
    # job was lost are queued again (a duplicate job is harmless: execute_run holds a per-run lock).
    runs = await deps.pool.fetch("SELECT id, status FROM research_runs WHERE status IN ('queued', 'running')")
    for r in runs:
        job_id = f"run:{r['id']}" if r["status"] == "queued" else f"run:{r['id']}:resume:{int(time.time())}"
        await redis.enqueue_job("research_run_job", str(r["id"]), _job_id=job_id)
    if runs:
        log.info("re-enqueued unfinished research runs", extra={"count": len(runs)})


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
    embedder, reranker = await asyncio.gather(
        asyncio.to_thread(load_embedder, cfg), asyncio.to_thread(load_reranker, cfg)
    )
    gpu_lock = asyncio.Lock()  # one GPU: pipeline embedding and search share it
    ctx["cfg"] = cfg
    ctx["search_models"] = SearchModels(embedder, reranker, cfg, lock=gpu_lock)
    ctx["deps"] = PipelineDeps(
        cfg=cfg,
        settings=settings,
        pool=pool,
        redis=get_redis(),
        grobid=GrobidClient(settings.grobid_url),
        embedder=embedder,
        gpu_lock=gpu_lock,
    )
    ctx["agent"] = AgentDeps(
        cfg=cfg,
        pool=pool,
        redis=get_redis(),
        llm=LLMClient(cfg, redis=get_redis(), call_logger=db_call_logger(get_sessionmaker())),
        models=ctx["search_models"],
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
        func(search_job, timeout=120, max_tries=1, keep_result=60),
        func(research_run_job, timeout=600, max_tries=1, keep_result=0),
        func(parse_paper, max_tries=MAX_PARSE_FAILURES + 1, timeout=900, keep_result=0),
        func(chunk_paper, max_tries=5, timeout=900, keep_result=0),
        func(resolve_refs_job, max_tries=3, timeout=900, keep_result=0),
        func(post_harvest_job, max_tries=1, timeout=6 * 3600, keep_result=3600),
        func(weekly_snapshot_job, max_tries=1, timeout=8 * 3600, keep_result=3600),
        func(digest_job, max_tries=1, timeout=1800, keep_result=3600),
        func(embed_abstracts_job, timeout=6 * 3600, max_tries=1),
        func(recompute_scores_job, timeout=3600, max_tries=1),
    ]
    cron_jobs: ClassVar[list[Any]] = [
        cron(janitor_job, minute={0, 30}, run_at_startup=True),
        cron(nightly_job, hour=2, minute=0, unique=True),  # container clock is UTC; a no-op while disabled
        cron(weekly_snapshot_job, weekday="mon", hour=0, minute=30, unique=True),  # before the 03:00 digests
        cron(digest_job, weekday="mon", hour=3, minute=0, unique=True),
    ]
    on_startup = startup
    on_shutdown = shutdown
    queue_name = ARQ_QUEUE
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    health_check_interval = 30
    max_jobs = 16  # pipeline jobs mostly wait on GROBID; keep slots free for searches
    poll_delay = 0.05  # arq's default 0.5s would add up to half a second to every search

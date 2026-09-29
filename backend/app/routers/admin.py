"""GET /admin/stats: pipeline state counts, fetch queues, throughput, failures, disk usage."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Awaitable
from datetime import datetime
from pathlib import Path
from typing import cast

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from core.config import get_settings
from core.db import get_engine
from core.redis import FETCH_BULK, FETCH_URGENT, get_redis

router = APIRouter(prefix="/admin", tags=["admin"])

STATS_CACHE_S = 30
STATEMENT_TIMEOUT = "20s"
DISK_CACHE_S = 60
DISK_SUBDIRS = ("parsed", "pdf_tmp", "models", "raw", "backups", "redis")
RECENT_FAILURES = 20


class PaperCounts(BaseModel):
    total: int
    by_state: dict[str, int]
    landmarks: int
    with_abstract_embedding: int
    venue_accepted: int
    code_available: int
    with_citation_data: int


class Throughput(BaseModel):
    ready_last_hour: int
    ready_last_day: int
    llm_calls_last_day: int
    llm_cost_usd_last_day: float
    llm_cost_usd_total: float


class Failure(BaseModel):
    arxiv_id: str
    title: str
    state: str
    fail_count: int
    last_error: str | None
    updated_at: datetime


class AdminStats(BaseModel):
    papers: PaperCounts
    fetch_queues: dict[str, int]
    throughput: Throughput
    recent_failures: list[Failure]
    disk_bytes: dict[str, int]
    generated_at: datetime


_disk_cache: tuple[float, dict[str, int]] | None = None


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


async def disk_usage() -> dict[str, int]:
    global _disk_cache
    if _disk_cache is not None and time.monotonic() - _disk_cache[0] < DISK_CACHE_S:
        return _disk_cache[1]
    data_dir = get_settings().data_dir
    sizes = await asyncio.gather(*(asyncio.to_thread(_dir_size, data_dir / d) for d in DISK_SUBDIRS))
    usage = dict(zip(DISK_SUBDIRS, sizes, strict=True))
    async with get_engine().connect() as conn:
        usage["postgres"] = int((await conn.execute(text("SELECT pg_database_size(current_database())"))).scalar_one())
    _disk_cache = (time.monotonic(), usage)
    return usage


_COUNTS = text("""
SELECT count(*) AS total,
       count(*) FILTER (WHERE is_landmark) AS landmarks,
       count(*) FILTER (WHERE abstract_embedding IS NOT NULL) AS with_abstract_embedding,
       count(*) FILTER (WHERE venue_accepted) AS venue_accepted,
       count(*) FILTER (WHERE code_available) AS code_available,
       count(*) FILTER (WHERE pagerank IS NOT NULL) AS with_citation_data,
       count(*) FILTER (WHERE state = 'ready' AND updated_at > now() - interval '1 hour') AS ready_last_hour,
       count(*) FILTER (WHERE state = 'ready' AND updated_at > now() - interval '1 day') AS ready_last_day
FROM papers
""")
_BY_STATE = text("SELECT state, count(*) FROM papers GROUP BY state ORDER BY state")
_LLM = text("""
SELECT count(*) FILTER (WHERE created_at > now() - interval '1 day') AS calls_day,
       coalesce(sum(cost_usd) FILTER (WHERE created_at > now() - interval '1 day'), 0) AS cost_day,
       coalesce(sum(cost_usd), 0) AS cost_total
FROM llm_calls
""")
_FAILURES = text("""
SELECT arxiv_id, title, state, fail_count, last_error, updated_at FROM papers
WHERE state IN ('failed', 'abstract_only') ORDER BY updated_at DESC LIMIT :limit
""")


async def _llen(key: str) -> int:
    return int(await cast(Awaitable[int], get_redis().llen(key)))


_stats_cache: tuple[float, AdminStats] | None = None
_stats_lock = asyncio.Lock()


@router.get("/stats", response_model=AdminStats)
async def stats() -> AdminStats:
    """Cached and single-flight: the counts scan the whole papers table, so at most one
    computation runs at a time and results are reused for STATS_CACHE_S."""
    global _stats_cache
    if _stats_cache is not None and time.monotonic() - _stats_cache[0] < STATS_CACHE_S:
        return _stats_cache[1]
    async with _stats_lock:
        if _stats_cache is not None and time.monotonic() - _stats_cache[0] < STATS_CACHE_S:
            return _stats_cache[1]
        result = await _compute_stats()
        _stats_cache = (time.monotonic(), result)
        return result


async def _compute_stats() -> AdminStats:
    async with get_engine().begin() as conn:
        await conn.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
        c = (await conn.execute(_COUNTS)).mappings().one()
        by_state = {row[0]: int(row[1]) for row in await conn.execute(_BY_STATE)}
        llm = (await conn.execute(_LLM)).mappings().one()
        failures = (await conn.execute(_FAILURES, {"limit": RECENT_FAILURES})).mappings().all()
    urgent, bulk = await asyncio.gather(_llen(FETCH_URGENT), _llen(FETCH_BULK))
    return AdminStats(
        papers=PaperCounts(
            total=c["total"],
            by_state=by_state,
            landmarks=c["landmarks"],
            with_abstract_embedding=c["with_abstract_embedding"],
            venue_accepted=c["venue_accepted"],
            code_available=c["code_available"],
            with_citation_data=c["with_citation_data"],
        ),
        fetch_queues={"urgent": int(urgent), "bulk": int(bulk)},
        throughput=Throughput(
            ready_last_hour=c["ready_last_hour"],
            ready_last_day=c["ready_last_day"],
            llm_calls_last_day=llm["calls_day"],
            llm_cost_usd_last_day=float(llm["cost_day"]),
            llm_cost_usd_total=float(llm["cost_total"]),
        ),
        recent_failures=[Failure.model_validate(dict(f)) for f in failures],
        disk_bytes=await disk_usage(),
        generated_at=datetime.now().astimezone(),
    )

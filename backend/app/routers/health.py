"""Liveness and readiness endpoints."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from core.config import get_settings
from core.db import get_engine
from core.redis import ARQ_HEALTH_KEY, FETCHER_HEARTBEAT, get_redis

router = APIRouter(tags=["health"])

CHECK_TIMEOUT_S = 5.0
HEARTBEAT_MAX_AGE_S = 90
REQUIRED_EXTENSIONS = {"vector", "pg_trgm"}


class CheckResult(BaseModel):
    ok: bool
    detail: str
    latency_ms: int


class ReadyResponse(BaseModel):
    status: str
    checks: dict[str, CheckResult]


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def check_postgres() -> str:
    async with get_engine().connect() as conn:
        exts: set[str] = set((await conn.execute(text("SELECT extname FROM pg_extension"))).scalars())
        missing = REQUIRED_EXTENSIONS - exts
        if missing:
            raise RuntimeError(f"missing extensions {sorted(missing)}; run `make migrate`")
        version: str = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    return f"migration {version}"


async def check_redis() -> str:
    await get_redis().ping()
    return "pong"


async def check_grobid() -> str:
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_S) as client:
        r = await client.get(f"{get_settings().grobid_url}/api/isalive")
    if r.status_code != 200 or r.text.strip().lower() != "true":
        raise RuntimeError(f"isalive returned {r.status_code} {r.text[:50]!r}")
    return "alive"


async def check_worker() -> str:
    value = await get_redis().get(ARQ_HEALTH_KEY)
    if value is None:
        raise RuntimeError("no arq health-check key (worker not running?)")
    return str(value)


async def check_fetcher() -> str:
    value = await get_redis().get(FETCHER_HEARTBEAT)
    if value is None:
        raise RuntimeError("no heartbeat (fetcher not running?)")
    age = time.time() - float(value)
    if age > HEARTBEAT_MAX_AGE_S:
        raise RuntimeError(f"heartbeat is {age:.0f}s old")
    return f"heartbeat {age:.0f}s ago"


async def _run(fn: Callable[[], Awaitable[str]]) -> CheckResult:
    start = time.perf_counter()
    try:
        detail = await asyncio.wait_for(fn(), CHECK_TIMEOUT_S)
        ok = True
    except Exception as e:
        detail, ok = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__, False
    return CheckResult(ok=ok, detail=detail, latency_ms=int((time.perf_counter() - start) * 1000))


CHECKS: dict[str, Callable[[], Awaitable[str]]] = {
    "postgres": check_postgres,
    "redis": check_redis,
    "grobid": check_grobid,
    "worker": check_worker,
    "fetcher": check_fetcher,
}


@router.get("/health/ready", response_model=ReadyResponse)
async def ready(request: Request, response: Response) -> ReadyResponse:
    names = list(CHECKS)
    results = await asyncio.gather(*(_run(CHECKS[n]) for n in names))
    checks = dict(zip(names, results, strict=True))
    models: Any = getattr(request.app.state, "llm_models", None)
    checks["llm"] = CheckResult(
        ok=bool(models), detail=f"models available: {', '.join(models)}" if models else "not checked", latency_ms=0
    )
    all_ok = all(c.ok for c in checks.values())
    if not all_ok:
        response.status_code = 503
    return ReadyResponse(status="ok" if all_ok else "degraded", checks=checks)

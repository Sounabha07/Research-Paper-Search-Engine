"""Shared Redis client and key names."""

from __future__ import annotations

from functools import lru_cache

from redis.asyncio import Redis

from core.config import get_settings

FETCH_URGENT = "fetch:urgent"
FETCH_BULK = "fetch:bulk"
FETCHER_HEARTBEAT = "fetcher:heartbeat"
ARQ_QUEUE = "arq:queue"
ARQ_HEALTH_KEY = f"{ARQ_QUEUE}:health-check"


def fetch_done_channel(arxiv_id: str) -> str:
    return f"fetch:done:{arxiv_id}"


def run_channel(run_id: str) -> str:
    return f"run:{run_id}"


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    client: Redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return client

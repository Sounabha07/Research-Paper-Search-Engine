"""Per-paper pipeline measurements (fetch/parse timings, chunks, resolution) kept in Redis for reports."""

from __future__ import annotations

import json
from collections.abc import Awaitable
from typing import Any, cast

from redis.asyncio import Redis

KEY_PREFIX = "pipeline:metrics:"
TTL_S = 30 * 86400


async def record(redis: Redis, arxiv_id: str, **fields: Any) -> None:
    key = KEY_PREFIX + arxiv_id
    await cast(Awaitable[int], redis.hset(key, mapping={k: json.dumps(v) for k, v in fields.items()}))
    await redis.expire(key, TTL_S)


async def load(redis: Redis, arxiv_id: str) -> dict[str, Any]:
    raw = await cast(Awaitable[dict[str, str]], redis.hgetall(KEY_PREFIX + arxiv_id))
    return {k: json.loads(v) for k, v in raw.items()}

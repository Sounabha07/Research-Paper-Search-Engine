"""Proactive per-model request limits for the Gemini free tier (requests per minute).

Every process (api, worker, eval scripts) takes a slot from one sliding 60 s window per model in Redis before
calling the API, so the combined rate stays under the limit instead of relying on 429 retries. Cache hits do
not take a slot. Without Redis (tests), a per-process window is used.
A per-model daily counter (Pacific-time day, as Google counts it) is kept for reporting.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

WINDOW_S = 60.0
PACIFIC = ZoneInfo("America/Los_Angeles")

# Atomically: drop entries older than the window; take a slot if one is free (returns "0"), else return how
# long until the oldest entry leaves the window.
_ACQUIRE = """
local key, now, window, limit, member = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
if redis.call('ZCARD', key) < limit then
  redis.call('ZADD', key, now, member)
  redis.call('PEXPIRE', key, math.ceil(window * 1000) + 1000)
  return '0'
end
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
return tostring(tonumber(oldest[2]) + window - now)
"""


def day_key(model: str, now: float | None = None) -> str:
    day = datetime.fromtimestamp(now if now is not None else time.time(), PACIFIC).strftime("%Y-%m-%d")
    return f"llm:rpd:{model}:{day}"


class RateLimiter:
    def __init__(
        self,
        limits: Mapping[str, int],
        redis: Redis | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.limits = dict(limits)
        self._redis = redis
        self._clock = clock
        self._sleep = sleep
        self._local: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()
        self.waited_s = 0.0  # total time spent waiting, for logs and tests

    async def _try(self, model: str, limit: int) -> float:
        """Take a slot now (returns 0) or return the seconds to wait before trying again."""
        now = self._clock()
        if self._redis is not None:
            wait = await self._redis.eval(  # type: ignore[misc]
                _ACQUIRE, 1, f"llm:rpm:{model}", str(now), str(WINDOW_S), str(limit), uuid.uuid4().hex
            )
            return float(wait.decode() if isinstance(wait, bytes) else wait)
        q = self._local.setdefault(model, deque())
        while q and q[0] <= now - WINDOW_S:
            q.popleft()
        if len(q) < limit:
            q.append(now)
            return 0.0
        return q[0] + WINDOW_S - now

    async def acquire(self, model: str) -> float:
        """Wait until a request to `model` is allowed; returns the time waited. Unlisted models are not limited."""
        limit = self.limits.get(model)
        if not limit:
            return 0.0
        waited = 0.0
        async with self._lock:  # one waiter per process at a time keeps the order fair
            while True:
                wait = await self._try(model, limit)
                if wait <= 0:
                    break
                wait = min(wait + 0.05, WINDOW_S)
                waited += wait
                await self._sleep(wait)
        self.waited_s += waited
        if self._redis is not None:
            key = day_key(model, self._clock())
            await self._redis.incr(key)
            await self._redis.expire(key, 3 * 86400)
        return waited

    async def used_today(self, model: str) -> int:
        if self._redis is None:
            return 0
        v = await self._redis.get(day_key(model, self._clock()))
        return int(v) if v else 0

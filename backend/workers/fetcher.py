"""The single process that talks to arXiv (spec 6.4). Plain asyncio loop, not arq.

Milestone 1: global rate limiter and liveness heartbeat. Queue consumption
(`BLPOP fetch:urgent fetch:bulk`) and fetch handlers arrive in milestone 3.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Awaitable, Callable

from core.config import ensure_data_dirs, get_config
from core.logging import setup_logging
from core.redis import FETCHER_HEARTBEAT, get_redis

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 30


class RateLimiter:
    """Enforces a minimum interval between any two requests, process-wide."""

    def __init__(
        self,
        min_interval_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last: float | None = None

    async def wait(self) -> None:
        async with self._lock:
            if self._last is not None:
                remaining = self.min_interval_s - (self._clock() - self._last)
                if remaining > 0:
                    await self._sleep(remaining)
            self._last = self._clock()


async def heartbeat(stop: asyncio.Event) -> None:
    redis = get_redis()
    while not stop.is_set():
        await redis.set(FETCHER_HEARTBEAT, str(time.time()), ex=HEARTBEAT_INTERVAL_S * 3)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), HEARTBEAT_INTERVAL_S)


async def main() -> None:
    setup_logging()
    cfg = get_config()
    ensure_data_dirs()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows dev runs
            loop.add_signal_handler(sig, stop.set)
    await get_redis().ping()
    log.info("fetcher started", extra={"min_interval_s": cfg.arxiv.min_interval_s})
    await heartbeat(stop)
    await get_redis().aclose()
    log.info("fetcher stopped")


if __name__ == "__main__":
    asyncio.run(main())

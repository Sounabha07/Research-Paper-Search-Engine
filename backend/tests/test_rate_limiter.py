from __future__ import annotations

import pytest

from workers.fetcher import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


async def test_enforces_min_interval() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3.1, clock=clock, sleep=clock.sleep)
    await limiter.wait()
    assert clock.sleeps == []
    clock.now += 1.0
    await limiter.wait()
    assert clock.sleeps == [pytest.approx(2.1)]
    clock.now += 5.0
    await limiter.wait()
    assert len(clock.sleeps) == 1

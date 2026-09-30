from __future__ import annotations

from core.llm import quota_value
from core.ratelimit import RateLimiter, day_key


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


async def test_limiter_spaces_requests_within_a_minute() -> None:
    clock = FakeClock()
    rl = RateLimiter({"lite": 3}, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        assert await rl.acquire("lite") == 0.0
        clock.t += 1
    waited = await rl.acquire("lite")  # 4th request in the same minute waits for the oldest to expire
    assert 56 < waited < 58
    assert clock.sleeps and rl.waited_s == waited
    assert await rl.acquire("other") == 0.0  # unlisted models are not limited


async def test_limiter_frees_slots_after_the_window() -> None:
    clock = FakeClock()
    rl = RateLimiter({"lite": 2}, clock=clock, sleep=clock.sleep)
    await rl.acquire("lite")
    await rl.acquire("lite")
    clock.t += 61
    assert await rl.acquire("lite") == 0.0


def test_day_key_uses_pacific_time() -> None:
    # 2026-09-30 05:00 UTC is still 2026-09-29 in California
    assert day_key("m", 1790744400.0) == "llm:rpd:m:2026-09-29"


def test_quota_value_from_error_text() -> None:
    err = RuntimeError("429 RESOURCE_EXHAUSTED ... 'quotaMetric': '...requests', 'quotaValue': '1000' ...")
    assert quota_value(err) == 1000
    assert quota_value(RuntimeError("503 overloaded")) is None

from __future__ import annotations

from agent.digest import keyword_phrase_sql
from agent.run import FetchPlan, fetch_goal
from core.config import AppConfig


def test_fetch_goal_rounds_up() -> None:
    assert fetch_goal(20, 0.6) == 12
    assert fetch_goal(20, 0.8) == 16
    assert fetch_goal(7, 0.6) == 5  # 4.2 -> 5
    assert fetch_goal(0, 0.8) == 0


def test_plan_queues_only_what_the_goal_needs() -> None:
    plan = FetchPlan([f"p{i}" for i in range(12)], ready=8, goal=16, spare=3)
    assert plan.next_batch() == [f"p{i}" for i in range(11)]  # 8 missing + 3 spare
    assert plan.next_batch() == []  # nothing more until something finishes
    plan.finish("p0", "ready")
    assert plan.next_batch() == []  # a success needs no replacement
    plan.finish("p1", "abstract_only")
    assert plan.next_batch() == ["p11"]  # a failure is replaced by the next candidate
    assert plan.ready == 9 and plan.failed == ["p1"] and not plan.reached


def test_plan_reached_and_exhausted() -> None:
    plan = FetchPlan(["a", "b"], ready=3, goal=4, spare=0)
    assert plan.next_batch() == ["a"]
    plan.finish("a", "abstract_only")
    assert plan.next_batch() == ["b"]
    plan.finish("b", "ready")
    assert plan.reached and plan.exhausted
    plan.finish("zzz", "ready")  # not pending: ignored
    assert plan.ready == 4

    stuck = FetchPlan(["a"], ready=0, goal=2, spare=1)
    stuck.next_batch()
    stuck.finish("a", "abstract_only")
    assert stuck.exhausted and not stuck.reached and stuck.next_batch() == []


def test_plan_already_reached() -> None:
    plan = FetchPlan(["a", "b"], ready=12, goal=12, spare=3)
    assert plan.reached and plan.next_batch() == []


def test_mode_targets(cfg: AppConfig) -> None:
    a = cfg.agent
    assert a.papers_for_answer == 20 and a.max_passages_per_paper == 4
    assert a.fetch_target_quick == 0.6 and a.fetch_target_deep == 0.8
    assert a.fetch_wait_quick_s < a.quick_timeout_s and a.fetch_wait_deep_s < a.deep_timeout_s
    assert cfg.retrieval.keyword_engine == "bm25"


def test_keyword_phrase_sql() -> None:
    assert keyword_phrase_sql(2, 4) == "p.title ### $4 OR p.abstract ### $4 OR p.title ### $5 OR p.abstract ### $5"


class FakeRedis:
    def __init__(self, urgent: list[str]) -> None:
        self.lists: dict[str, list[str]] = {"fetch:urgent": list(urgent), "fetch:bulk": []}

    async def lrem(self, key: str, count: int, value: str) -> int:
        before = len(self.lists[key])
        self.lists[key] = [v for v in self.lists[key] if v != value]
        return before - len(self.lists[key])

    async def rpush(self, key: str, *values: str) -> int:
        self.lists[key] += values
        return len(self.lists[key])


async def test_demote_moves_only_queued_urgent_jobs() -> None:
    from core.fetchq import demote_fulltext, pdf_job

    r = FakeRedis([pdf_job("a"), pdf_job("other"), pdf_job("b")])
    assert await demote_fulltext(r, ["a", "b", "taken-by-fetcher"]) == 2  # type: ignore[arg-type]
    assert r.lists["fetch:urgent"] == [pdf_job("other")]
    assert r.lists["fetch:bulk"] == [pdf_job("a"), pdf_job("b")]


def test_weekly_cron_keeps_its_long_timeout() -> None:
    from workers.arq_worker import WEEKLY_TIMEOUT_S, WorkerSettings

    weekly = next(c for c in WorkerSettings.cron_jobs if c.name == "cron:weekly_snapshot_job")
    assert weekly.timeout_s == WEEKLY_TIMEOUT_S  # not arq's 300 s default for cron entries

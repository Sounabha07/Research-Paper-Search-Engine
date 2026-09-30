from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from agent.run import RunState, _filters
from core.config import AppConfig


class FakePool:
    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = steps
        self.executed: list[str] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(sql)
        return "DELETE 0"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [{"seq": s["seq"], "type": s["type"], "payload": json.dumps(s["payload"])} for s in self.steps]


def _run(cfg: AppConfig, mode: str, started_ago_s: float | None) -> dict[str, Any]:
    started = datetime.now(UTC) - timedelta(seconds=started_ago_s) if started_ago_s is not None else None
    return {"id": uuid.uuid4(), "thread_id": uuid.uuid4(), "turn": 1, "question": "q", "mode": mode,
            "filters": json.dumps({"restrict_ids": ["2106.09685"]}), "started_at": started}  # fmt: skip


async def test_resume_loads_completed_steps(cfg: AppConfig) -> None:
    steps = [
        {"seq": 1, "type": "rewrite", "payload": {"standalone_question": "q", "queries": ["a"]}},
        {"seq": 2, "type": "find_papers", "payload": {"papers": []}},
        {"seq": 3, "type": "fetch", "payload": {"requested": 2}},
    ]
    pool = FakePool(steps)
    st = RunState(SimpleNamespace(cfg=cfg, pool=pool), _run(cfg, "deep", 10))  # type: ignore[arg-type]
    await st.load_progress()
    assert set(st.done) == {"rewrite", "find_papers", "fetch"} and st.seq == 3
    assert "verify" in pool.executed[0] and "error" in pool.executed[0]  # stale verify/consensus/error rows cleared


def test_deadline_counts_from_first_start(cfg: AppConfig) -> None:
    deps = SimpleNamespace(cfg=cfg, pool=None)
    fresh = RunState(deps, _run(cfg, "deep", None))  # type: ignore[arg-type]
    resumed = RunState(deps, _run(cfg, "deep", cfg.agent.deep_timeout_s + 5))  # type: ignore[arg-type]
    assert not fresh.past_deadline()
    assert resumed.past_deadline()  # the budget counts from the first start


async def test_resumed_run_gets_grace(cfg: AppConfig) -> None:
    steps = [{"seq": 1, "type": "rewrite", "payload": {"standalone_question": "q", "queries": ["a"]}}]
    st = RunState(SimpleNamespace(cfg=cfg, pool=FakePool(steps)), _run(cfg, "deep", cfg.agent.deep_timeout_s + 90))  # type: ignore[arg-type]
    await st.load_progress()
    assert not st.past_deadline()  # restart time does not use up the resumed run's budget
    fresh = RunState(SimpleNamespace(cfg=cfg, pool=FakePool([])), _run(cfg, "deep", cfg.agent.deep_timeout_s + 90))  # type: ignore[arg-type]
    await fresh.load_progress()
    assert fresh.past_deadline()  # no completed steps: no grace


def test_filters_carry_restrict_ids() -> None:
    assert _filters(json.dumps({"restrict_ids": ["x"], "year_from": 2020})).restrict_ids == ("x",)

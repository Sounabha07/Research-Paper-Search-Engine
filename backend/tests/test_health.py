from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import health


async def _ok() -> str:
    return "fine"


async def _fail() -> str:
    raise RuntimeError("down")


def _app(models: list[str] | None) -> FastAPI:
    app = FastAPI()
    app.include_router(health.router)
    app.state.llm_models = models
    return app


def test_liveness() -> None:
    assert TestClient(_app(None)).get("/health").json() == {"status": "ok"}


def test_ready_all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "CHECKS", {"a": _ok, "b": _ok})
    r = TestClient(_app(["m"])).get("/health/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert set(r.json()["checks"]) == {"a", "b", "llm"}


def test_ready_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "CHECKS", {"a": _ok, "b": _fail})
    r = TestClient(_app(["m"])).get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["b"]["ok"] is False
    assert body["checks"]["b"]["detail"] == "RuntimeError: down"

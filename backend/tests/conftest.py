from __future__ import annotations

from pathlib import Path

import pytest

from core.config import AppConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setenv("CONTACT_EMAIL", "test@example.com")
    return load_config(REPO_ROOT / "config.yaml")

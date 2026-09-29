from __future__ import annotations

from pathlib import Path

import pytest

from core.config import AppConfig, ConfigError, load_config, require_env
from tests.conftest import REPO_ROOT


def test_repo_config_loads(cfg: AppConfig) -> None:
    assert cfg.corpus.categories_prefixes == ["cs.", "math."]
    assert cfg.models.embedding == "BAAI/bge-small-en-v1.5"
    assert cfg.retrieval.rerank_min_score is None
    assert cfg.arxiv.user_agent == "arxiv-research-engine/0.1 (mailto:test@example.com)"
    assert abs(sum(cfg.score_weights.model_dump().values()) - 1.0) < 1e-9
    assert len(cfg.venue_patterns) == 5


def test_missing_env_var_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTACT_EMAIL", raising=False)
    with pytest.raises(ConfigError, match="CONTACT_EMAIL"):
        load_config(REPO_ROOT / "config.yaml")


def test_require_env_empty_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "  ")
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        require_env("GEMINI_API_KEY")


def test_unknown_key_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTACT_EMAIL", "x@example.com")
    text = (REPO_ROOT / "config.yaml").read_text(encoding="utf-8")
    p = tmp_path / "config.yaml"
    p.write_text(text.replace("rrf_k: 60", "rrf_k: 60\n  rrf_kk: 1"), encoding="utf-8")
    with pytest.raises(ConfigError, match="rrf_kk"):
        load_config(p)


def test_landmarks_file_exists(cfg: AppConfig) -> None:
    assert (REPO_ROOT / cfg.corpus.landmarks_file).is_file()


def test_paper_index_definitions_match_migration() -> None:
    from ingest.indexes import PAPER_INDEXES

    migration = (REPO_ROOT / "backend" / "migrations" / "versions" / "0001_initial.py").read_text(encoding="utf-8")
    flat = " ".join(migration.replace('"\n    "', "").split())
    for name, ddl in PAPER_INDEXES.items():
        body = ddl.replace("IF NOT EXISTS ", "")
        assert " ".join(body.split()) in flat, name

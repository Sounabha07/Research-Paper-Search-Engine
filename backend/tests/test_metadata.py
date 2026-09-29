from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.config import AppConfig
from ingest import kaggle
from ingest.landmarks import Landmark, load_landmarks
from ingest.metadata import MetadataPass, PaperRow, format_counts, parse_authors, parse_v1_date, window_cutoff
from tests.conftest import REPO_ROOT

FIXTURE = Path(__file__).parent / "fixtures" / "kaggle_sample.jsonl"
NOW = datetime(2026, 9, 29, tzinfo=UTC)
LANDMARKS = [
    Landmark("1706.03762", "Attention Is All You Need"),
    Landmark("1412.6980", "Adam: A Method for Stochastic Optimization"),
    Landmark("math/0211159", "The entropy formula for the Ricci flow and its geometric applications"),
    Landmark("9999.99999", "Not in the snapshot"),
]


@pytest.fixture
def zip_path(tmp_path: Path) -> Path:
    p = tmp_path / kaggle.ZIP_NAME
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(FIXTURE, kaggle.MEMBER)
    return p


def _run(cfg: AppConfig, zip_path: Path) -> tuple[MetadataPass, dict[str, PaperRow]]:
    mp = MetadataPass(cfg, LANDMARKS, now=NOW)
    rows = {}
    for rec in kaggle.iter_records(zip_path):
        row = mp.process(rec)
        if row is not None:
            rows[row.arxiv_id] = row
    mp.finish()
    return mp, rows


def test_stream_filter_and_landmarks(cfg: AppConfig, zip_path: Path) -> None:
    assert kaggle.is_valid_zip(zip_path)
    mp, rows = _run(cfg, zip_path)
    c = mp.counts
    # in-window cs + math cross-list, plus two validated old landmarks
    assert set(rows) == {"2401.00001", "2310.00002", "1706.03762", "math/0211159"}
    assert c.lines == 8
    assert c.parse_errors == 1
    assert c.cs_math_any_date == 6  # physics paper excluded
    assert c.in_window == 2
    assert c.landmarks_validated == 2
    assert c.landmarks_outside_window == 2
    assert [m["id"] for m in c.landmark_mismatches] == ["1412.6980"]
    assert c.landmarks_missing == ["9999.99999"]
    assert c.tier1 == 4
    assert c.tier1_by_archive["cs"] == 2 and c.tier1_by_archive["math"] == 2
    text = format_counts(c, mp.cutoff, dry_run=True)
    assert "TIER 1 TOTAL" in text and "MISMATCH 1412.6980" in text and "MISSING  9999.99999" in text


def test_row_fields(cfg: AppConfig, zip_path: Path) -> None:
    _, rows = _run(cfg, zip_path)
    r = rows["2401.00001"]
    assert r.title == "A Fast Method for Graphs"
    assert r.title_norm == "afastmethodforgraphs"
    assert r.abstract == "We propose a method. Code: https://github.com/ann/fast"
    assert r.primary_category == "cs.LG"
    assert r.v1_date == datetime(2024, 1, 1, 10, tzinfo=UTC)  # v1, not the v2 or update_date
    assert r.venue_accepted and r.venue_name == "NeurIPS 2024"
    assert r.code_available and not r.is_landmark
    assert r.authors == [{"first": "Ann", "last": "Lee"}, {"first": "Bo", "last": "Chen Jr."}]

    m = rows["2310.00002"]
    assert m.primary_category == "stat.TH"
    assert m.venue_accepted and m.venue_name == "Annals of Mathematics"
    assert m.badges["is_survey"] is True

    assert rows["1706.03762"].is_landmark and rows["1706.03762"].badges["landmark"] is True


def test_parse_helpers() -> None:
    assert parse_v1_date({"versions": []}) is None
    assert parse_v1_date({"versions": [{"created": "garbage"}]}) is None
    assert parse_authors({"authors_parsed": [["Solo"]]}) == [{"first": "", "last": "Solo"}]
    assert window_cutoff(5, datetime(2028, 2, 29, tzinfo=UTC)) == datetime(2023, 2, 28, tzinfo=UTC)


def test_invalid_zip(tmp_path: Path) -> None:
    bad = tmp_path / "arxiv.zip"
    bad.write_bytes(b"partial download")
    assert not kaggle.is_valid_zip(bad)


def test_kaggle_env_prefers_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAGGLE_API_TOKEN", "tok")
    monkeypatch.setenv("KAGGLE_USERNAME", "u")
    monkeypatch.setenv("KAGGLE_KEY", "k")
    env = kaggle.kaggle_env()
    assert env["KAGGLE_API_TOKEN"] == "tok" and "KAGGLE_KEY" not in env
    monkeypatch.setenv("KAGGLE_API_TOKEN", "")
    env = kaggle.kaggle_env()
    assert env["KAGGLE_USERNAME"] == "u" and "KAGGLE_API_TOKEN" not in env
    monkeypatch.delenv("KAGGLE_KEY")
    with pytest.raises(Exception, match="KAGGLE_API_TOKEN"):
        kaggle.kaggle_env()


def test_seed_file_loads(cfg: AppConfig) -> None:
    landmarks = load_landmarks(REPO_ROOT / cfg.corpus.landmarks_file)
    assert len(landmarks) > 100
    assert len({lm.arxiv_id for lm in landmarks}) == len(landmarks)

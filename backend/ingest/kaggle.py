"""Kaggle arXiv metadata snapshot (spec 6.1): download and stream from the zip, never unzip."""

from __future__ import annotations

import logging
import os
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson

from core.config import ConfigError

log = logging.getLogger(__name__)

DATASET = "Cornell-University/arxiv"
ZIP_NAME = "arxiv.zip"
MEMBER = "arxiv-metadata-oai-snapshot.json"


def kaggle_env() -> dict[str, str]:
    """Environment for the kaggle CLI: KAGGLE_API_TOKEN if set, else legacy username + key."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("KAGGLE_")}
    token = os.environ.get("KAGGLE_API_TOKEN", "").strip()
    if token:
        env["KAGGLE_API_TOKEN"] = token
        return env
    user = os.environ.get("KAGGLE_USERNAME", "").strip()
    key = os.environ.get("KAGGLE_KEY", "").strip()
    if user and key:
        env["KAGGLE_USERNAME"] = user
        env["KAGGLE_KEY"] = key
        return env
    raise ConfigError(
        "Missing Kaggle credentials: set KAGGLE_API_TOKEN, or both KAGGLE_USERNAME and KAGGLE_KEY, in .env."
    )


def is_valid_zip(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            return MEMBER in zf.namelist()
    except zipfile.BadZipFile:
        return False


def download(raw_dir: Path) -> Path:
    """Run the kaggle CLI. It skips the download when the local zip is already current."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / ZIP_NAME
    if path.exists() and not is_valid_zip(path):
        log.warning("removing corrupt or partial kaggle zip", extra={"path": str(path)})
        path.unlink()
    cmd = ["kaggle", "datasets", "download", "-d", DATASET, "-p", str(raw_dir)]
    log.info("kaggle download", extra={"cmd": " ".join(cmd)})
    subprocess.run(cmd, env=kaggle_env(), check=True)
    if not is_valid_zip(path):
        raise RuntimeError(f"Kaggle download did not produce a valid {path} containing {MEMBER}")
    return path


def iter_records(zip_path: Path) -> Iterator[dict[str, Any] | None]:
    """Yield one dict per JSONL line, or None for a line that fails to parse."""
    with zipfile.ZipFile(zip_path) as zf, zf.open(MEMBER) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                yield orjson.loads(line)
            except orjson.JSONDecodeError:
                yield None

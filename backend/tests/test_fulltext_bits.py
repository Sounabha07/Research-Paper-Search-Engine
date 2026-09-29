from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pymupdf
import pytest

from core import fetchq
from ingest.citations import pagerank
from ingest.janitor import run_janitor
from ingest.pdftext import parse_pdf
from ingest.references import _pick
from retrieval.papers import rrf_fuse


def test_pagerank_simple_graph() -> None:
    # 0 -> 2, 1 -> 2, 2 -> 3 ; 3 dangling
    rank = pagerank(4, np.array([0, 1, 2]), np.array([2, 2, 3]))
    assert rank.sum() == pytest.approx(1.0)
    assert rank[3] > rank[2] > rank[0] == pytest.approx(rank[1])


def test_rrf_fuse() -> None:
    fused = rrf_fuse([["a", "b", "c"], ["b", "d"]], k=60)
    assert [d for d, _ in fused][:2] == ["b", "a"]
    assert dict(fused)["b"] == pytest.approx(1 / 62 + 1 / 61)


def test_pick_prefers_year() -> None:
    assert _pick([("x", 2019), ("y", 2023)], 2022) == "y"
    assert _pick([("x", 2019)], None) == "x"


class FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "state = 'pdf_fetched'" in sql:
            return [r for r in self.rows if r["state"] == "pdf_fetched"]
        wanted = set(args[0])
        return [r for r in self.rows if r["arxiv_id"] in wanted]

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "UPDATE 1"


class FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}

    async def rpush(self, key: str, *values: str) -> int:
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])


async def test_janitor_orphans_and_cap(tmp_path: Path) -> None:
    now = time.time()

    def make(name: str, size: int, age_h: float) -> Path:
        p = tmp_path / name
        p.write_bytes(b"x" * size)
        os.utime(p, (now - age_h * 3600, now - age_h * 3600))
        return p

    old_orphan = make("1111.11111.pdf", 10, 30)
    new_orphan = make("2222.22222.pdf", 10, 1)
    waiting_old = make("3333.33333.pdf", 600, 5)
    waiting_new = make("math_0211159.pdf", 600, 1)
    conn = FakeConn([
        {"arxiv_id": "3333.33333", "state": "pdf_fetched"},
        {"arxiv_id": "math/0211159", "state": "pdf_fetched"},
    ])  # fmt: skip
    res = await run_janitor(conn, tmp_path, cap_bytes=1000, now=now)  # type: ignore[arg-type,unused-ignore]
    assert not old_orphan.exists() and new_orphan.exists()  # orphans only after 24h
    assert not waiting_old.exists() and waiting_new.exists()  # over cap: oldest waiting PDF evicted
    assert res.orphans_deleted == 1 and res.over_cap_deleted == 1 and res.bytes_after == 600
    assert conn.executed[0][1] == ("3333.33333",)


async def test_enqueue_fulltext_outcomes() -> None:
    conn = FakeConn([
        {"arxiv_id": "a", "state": "signals_done", "fail_count": 0},
        {"arxiv_id": "b", "state": "ready", "fail_count": 0},
        {"arxiv_id": "c", "state": "queued_fulltext", "fail_count": 0},
        {"arxiv_id": "d", "state": "abstract_only", "fail_count": 3},
    ])  # fmt: skip
    redis = FakeRedis()
    out = await fetchq.enqueue_fulltext(conn, redis, ["a", "b", "c", "d", "zz"], urgent=True)  # type: ignore[arg-type]
    assert out == {"a": "queued", "b": "already_ready", "c": "in_progress", "d": "abstract_only", "zz": "not_found"}
    # urgent re-pushes the paper already waiting in bulk so it jumps ahead
    assert [fetchq.parse_job(j)["arxiv_id"] for j in redis.lists["fetch:urgent"]] == ["a", "c"]
    assert conn.executed and conn.executed[0][1][0] == ["a", "c"]


async def test_enqueue_meta_batches() -> None:
    redis = FakeRedis()
    n = await fetchq.enqueue_meta(redis, [f"{i:04d}.00001" for i in range(250)])  # type: ignore[arg-type]
    assert n == 3
    assert [len(fetchq.parse_job(j)["ids"]) for j in redis.lists["fetch:bulk"]] == [100, 100, 50]


def _make_pdf() -> bytes:
    doc = pymupdf.open()  # type: ignore[no-untyped-call]
    page = doc.new_page()
    y = 72.0
    for text, size in [
        ("A Tiny Paper About Graphs", 20),
        ("Abstract", 13),
        ("We study graphs. The results are good.", 10),
        ("1 Introduction", 13),
        ("Graphs are everywhere. We prove a theorem about them.", 10),
        ("References", 13),
        ("[1] A. Author. Some result. arXiv:2106.09685, 2021.", 10),
        ("[2] B. Author. Another result. 2019.", 10),
    ]:
        page.insert_text((72, y), text, fontsize=size)
        y += size * 2.2
    data = doc.tobytes()  # type: ignore[no-untyped-call]
    doc.close()  # type: ignore[no-untyped-call]
    return bytes(data)


def test_pymupdf_fallback_parser() -> None:
    doc = parse_pdf(_make_pdf())
    heads = [s.head for s in doc.sections]
    assert "Abstract" in heads and "1 Introduction" in heads
    body = " ".join(s for sec in doc.sections for p in sec.paragraphs for s in p)
    assert "Graphs are everywhere." in body and "arXiv:2106.09685" not in body  # references kept out
    assert [r.arxiv_id for r in doc.references] == ["2106.09685", None]
    assert doc.references[1].year == 2019

"""pdf_tmp janitor (spec 8.6): delete orphans older than 24h and cap the directory at 4GB.

A PDF is an orphan when its paper is no longer waiting to be parsed (state other than
pdf_fetched), or the file does not belong to any paper. Over the cap, the oldest waiting
PDFs are deleted too and their papers go back to signals_done so they can be re-requested.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from core.arxiv import file_stem

log = logging.getLogger(__name__)

CAP_BYTES = 4 * 1024**3
ORPHAN_AGE_S = 24 * 3600


@dataclass
class JanitorResult:
    orphans_deleted: int = 0
    over_cap_deleted: int = 0
    bytes_after: int = 0


def _list_files(pdf_dir: Path) -> list[tuple[Path, os.stat_result]]:
    return [(p, p.stat()) for p in pdf_dir.glob("*") if p.is_file()]


async def run_janitor(
    conn: asyncpg.Connection, pdf_dir: Path, cap_bytes: int = CAP_BYTES, now: float | None = None
) -> JanitorResult:
    now = now or time.time()
    res = JanitorResult()
    files = await asyncio.to_thread(_list_files, pdf_dir)
    waiting = {
        file_stem(r["arxiv_id"]): r["arxiv_id"]
        for r in await conn.fetch("SELECT arxiv_id FROM papers WHERE state = 'pdf_fetched'")
    }
    kept: list[tuple[Path, float, int, str]] = []
    for path, st in files:
        stem = path.name.removesuffix(".pdf").removesuffix(".part")
        if stem not in waiting or path.suffix == ".part":
            if now - st.st_mtime > ORPHAN_AGE_S:
                await asyncio.to_thread(path.unlink, missing_ok=True)
                res.orphans_deleted += 1
            continue
        kept.append((path, st.st_mtime, st.st_size, waiting[stem]))

    total = sum(size for *_, size, _ in kept)
    for path, _, size, arxiv_id in sorted(kept, key=lambda k: k[1]):  # oldest first
        if total <= cap_bytes:
            break
        await asyncio.to_thread(path.unlink, missing_ok=True)
        total -= size
        res.over_cap_deleted += 1
        await conn.execute(
            "UPDATE papers SET state = 'signals_done', last_error = 'pdf evicted by janitor (pdf_tmp over cap)'"
            " WHERE arxiv_id = $1 AND state = 'pdf_fetched'",
            arxiv_id,
        )
    res.bytes_after = total
    if res.orphans_deleted or res.over_cap_deleted:
        log.info("janitor", extra=res.__dict__)
    return res

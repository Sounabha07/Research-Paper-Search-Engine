"""Weekly Kaggle snapshot job (replaces the nightly OAI-PMH harvest, which arXiv refuses with HTTP 406).

1. `kaggle datasets download` (the CLI skips it when the local zip is current). If the zip did not change and a
   snapshot was ingested before, stop.
2. Stream the snapshot, processing only records whose update_date is on or after the newest update_date of the
   last ingested snapshot (new and changed papers); they go through the same metadata pass and upsert as the bulk
   load, and new records are upserted into the arXiv catalog.
3. Post-harvest steps: foundational papers, abstract embeddings, scores and PageRank.

`python -m ingest.weekly` runs it now (make snapshot); the worker runs it Mondays 00:30 UTC.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from core.config import AppConfig, get_config, get_settings
from core.logging import setup_logging
from core.redis import get_redis
from ingest import kaggle
from ingest.metadata import run as metadata_run
from ingest.nightly import post_harvest

log = logging.getLogger(__name__)

SNAPSHOT_LAST_UPDATE = "snapshot:last_update_date"  # newest update_date of the last ingested snapshot
SNAPSHOT_ZIP_STAMP = "snapshot:zip_stamp"  # size and mtime of the zip that was ingested


def zip_stamp(path: Path) -> str | None:
    if not path.exists():
        return None
    st = path.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


async def weekly_snapshot(cfg: AppConfig) -> str:
    redis = get_redis()
    raw_dir = get_settings().data_dir / "raw"
    t0 = time.monotonic()
    zip_path = await asyncio.to_thread(kaggle.download, raw_dir)
    download_s = time.monotonic() - t0
    stamp = zip_stamp(zip_path)
    last = await redis.get(SNAPSHOT_LAST_UPDATE)
    last = last.decode() if isinstance(last, bytes) else last
    prev_stamp = await redis.get(SNAPSHOT_ZIP_STAMP)
    prev_stamp = prev_stamp.decode() if isinstance(prev_stamp, bytes) else prev_stamp
    if last and stamp == prev_stamp:
        msg = f"snapshot unchanged ({stamp}); nothing to ingest (download check {download_s:.0f}s)"
        log.info("weekly snapshot unchanged", extra={"stamp": stamp})
        return msg

    t1 = time.monotonic()
    counts = await metadata_run(zip_path, cfg, dry_run=False, since=last)
    ingest_s = time.monotonic() - t1
    if counts.max_update_date:
        await redis.set(SNAPSHOT_LAST_UPDATE, counts.max_update_date)
    if stamp:
        await redis.set(SNAPSHOT_ZIP_STAMP, stamp)
    summary = (
        f"weekly snapshot: since {last or '(first run: all records)'}; lines {counts.lines:,}, skipped as unchanged "
        f"{counts.skipped_unchanged:,}, in window {counts.in_window:,}, inserted {counts.inserted:,}, updated "
        f"{counts.updated:,}, unchanged {counts.unchanged:,}; newest update_date {counts.max_update_date}; "
        f"download {download_s:.0f}s, ingest {ingest_s:.0f}s"
    )
    log.info("weekly snapshot ingested", extra={"since": last, "inserted": counts.inserted, "updated": counts.updated})
    return summary + "\n" + await post_harvest(cfg)


async def main() -> None:
    setup_logging()
    try:
        print(await weekly_snapshot(get_config()), flush=True)
    finally:
        await get_redis().aclose()


if __name__ == "__main__":
    asyncio.run(main())

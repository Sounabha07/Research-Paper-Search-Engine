"""Fetch queue (spec 6.4): jobs for the single arXiv fetcher process.

Queues are Redis lists popped with `BLPOP fetch:urgent fetch:bulk`. Jobs are JSON:
  {"type": "pdf", "arxiv_id": "2106.09685"}
  {"type": "meta", "ids": ["1234.5678", ...]}        (spec 6.2b, <= 100 ids)
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Iterable
from typing import Any, cast

import asyncpg
from redis.asyncio import Redis

from core.redis import FETCH_BULK, FETCH_URGENT

# A paper can be (re)queued for full text only from these states.
ENQUEUEABLE = ("metadata_done", "signals_done", "failed", "discovered")
IN_PROGRESS = ("queued_fulltext", "pdf_fetched", "parsed", "chunked", "embedded")
MAX_META_IDS = 100


def pdf_job(arxiv_id: str) -> str:
    return json.dumps({"type": "pdf", "arxiv_id": arxiv_id})


def meta_job(ids: list[str]) -> str:
    return json.dumps({"type": "meta", "ids": ids})


def parse_job(raw: str) -> dict[str, Any]:
    job = json.loads(raw)
    if job.get("type") not in ("pdf", "meta"):
        raise ValueError(f"unknown fetch job {raw!r}")
    return cast(dict[str, Any], job)


async def _rpush(redis: Redis, queue: str, *values: str) -> None:
    await cast(Awaitable[int], redis.rpush(queue, *values))


async def enqueue_fulltext(
    conn: asyncpg.Connection, redis: Redis, arxiv_ids: Iterable[str], urgent: bool
) -> dict[str, str]:
    """Queue papers for full text. Returns arxiv_id -> outcome.

    Outcomes: queued, already_ready, in_progress, abstract_only, not_found.
    Urgent requests re-push papers already waiting in the bulk queue so they jump ahead;
    the fetcher skips duplicates by checking state.
    """
    ids = list(dict.fromkeys(arxiv_ids))
    rows = await conn.fetch("SELECT arxiv_id, state, fail_count FROM papers WHERE arxiv_id = ANY($1::text[])", ids)
    by_id = {r["arxiv_id"]: r for r in rows}
    outcome: dict[str, str] = {}
    to_queue: list[str] = []
    for arxiv_id in ids:
        r = by_id.get(arxiv_id)
        if r is None:
            outcome[arxiv_id] = "not_found"
        elif r["state"] == "ready":
            outcome[arxiv_id] = "already_ready"
        elif r["state"] == "abstract_only":
            outcome[arxiv_id] = "abstract_only"
        elif r["state"] in IN_PROGRESS:
            outcome[arxiv_id] = "in_progress"
            if urgent and r["state"] == "queued_fulltext":
                to_queue.append(arxiv_id)
        else:
            outcome[arxiv_id] = "queued"
            to_queue.append(arxiv_id)
    if to_queue:
        await conn.execute(
            "UPDATE papers SET state = 'queued_fulltext' WHERE arxiv_id = ANY($1::text[]) AND state = ANY($2::text[])",
            to_queue,
            list(ENQUEUEABLE),
        )
        await _rpush(redis, FETCH_URGENT if urgent else FETCH_BULK, *(pdf_job(i) for i in to_queue))
    return outcome


async def enqueue_meta(redis: Redis, arxiv_ids: Iterable[str]) -> int:
    """Queue metadata lookups for referenced arXiv ids missing from the DB (bulk priority)."""
    ids = list(dict.fromkeys(arxiv_ids))
    batches = [ids[i : i + MAX_META_IDS] for i in range(0, len(ids), MAX_META_IDS)]
    if batches:
        await _rpush(redis, FETCH_BULK, *(meta_job(b) for b in batches))
    return len(batches)

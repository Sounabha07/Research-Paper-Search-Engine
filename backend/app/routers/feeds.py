"""Feeds: saved interests with weekly digests of new papers."""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg
from arq.connections import ArqRedis
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

router = APIRouter(tags=["feeds"])

DIGEST_TIMEOUT_S = 300


class FeedIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    query_text: str = Field(min_length=3, max_length=1000)
    keywords: list[str] = Field(default_factory=list, max_length=30)


class FeedPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    query_text: str | None = Field(default=None, min_length=3, max_length=1000)
    keywords: list[str] | None = Field(default=None, max_length=30)


def _pool(request: Request) -> asyncpg.Pool:
    pool: asyncpg.Pool | None = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool not ready")
    return pool


def _clean(keywords: list[str]) -> list[str]:
    return list(dict.fromkeys(k.strip() for k in keywords if k.strip()))


_COLS = "id, name, query_text, keywords, created_at, updated_at"


@router.get("/feeds")
async def list_feeds(request: Request) -> list[dict[str, Any]]:
    rows = await _pool(request).fetch(
        f"SELECT {', '.join('f.' + c for c in _COLS.split(', '))},"
        " (SELECT max(period) FROM digests d WHERE d.feed_id = f.id) AS last_period"
        " FROM feeds f ORDER BY f.created_at"
    )
    return [dict(r) for r in rows]


@router.post("/feeds", status_code=201)
async def create_feed(body: FeedIn, request: Request) -> dict[str, Any]:
    row = await _pool(request).fetchrow(
        f"INSERT INTO feeds (name, query_text, keywords) VALUES ($1, $2, $3) RETURNING {_COLS}",
        body.name.strip(), body.query_text.strip(), _clean(body.keywords),
    )  # fmt: skip
    return dict(row) if row else {}


@router.get("/feeds/{feed_id}")
async def get_feed(feed_id: uuid.UUID, request: Request) -> dict[str, Any]:
    row = await _pool(request).fetchrow(f"SELECT {_COLS} FROM feeds WHERE id = $1", feed_id)
    if row is None:
        raise HTTPException(status_code=404, detail="feed not found")
    return dict(row)


@router.patch("/feeds/{feed_id}")
async def update_feed(feed_id: uuid.UUID, body: FeedPatch, request: Request) -> dict[str, Any]:
    pool = _pool(request)
    f = body.model_dump(exclude_unset=True)
    row = await pool.fetchrow(
        "UPDATE feeds SET name = coalesce($2, name), query_text = coalesce($3, query_text),"
        " keywords = coalesce($4, keywords),"
        " query_embedding = CASE WHEN $3 IS NOT NULL AND $3 <> query_text THEN NULL ELSE query_embedding END"
        f" WHERE id = $1 RETURNING {_COLS}",
        feed_id, (f.get("name") or "").strip() or None, (f.get("query_text") or "").strip() or None,
        _clean(f["keywords"]) if f.get("keywords") is not None else None,
    )  # fmt: skip
    if row is None:
        raise HTTPException(status_code=404, detail="feed not found")
    return dict(row)


@router.delete("/feeds/{feed_id}", status_code=204)
async def delete_feed(feed_id: uuid.UUID, request: Request) -> Response:
    if await _pool(request).fetchval("DELETE FROM feeds WHERE id = $1 RETURNING id", feed_id) is None:
        raise HTTPException(status_code=404, detail="feed not found")
    return Response(status_code=204)


@router.get("/feeds/{feed_id}/digests")
async def list_digests(feed_id: uuid.UUID, request: Request, limit: int = 12) -> list[dict[str, Any]]:
    rows = await _pool(request).fetch(
        "SELECT period, content, created_at FROM digests WHERE feed_id = $1 ORDER BY period DESC LIMIT $2",
        feed_id, max(1, min(limit, 52)),
    )  # fmt: skip
    return [{**dict(r), "content": json.loads(r["content"]) if isinstance(r["content"], str) else r["content"]}
            for r in rows]  # fmt: skip


@router.post("/feeds/{feed_id}/digests")
async def run_digest(feed_id: uuid.UUID, request: Request, days: int | None = None) -> dict[str, Any]:
    """Build (or rebuild) this week's digest now instead of waiting for Monday. Runs in the worker (embeddings)."""
    arq: ArqRedis | None = getattr(request.app.state, "arq", None)
    if arq is None:
        raise HTTPException(status_code=503, detail="job queue not connected")
    if not await _pool(request).fetchval("SELECT 1 FROM feeds WHERE id = $1", feed_id):
        raise HTTPException(status_code=404, detail="feed not found")
    job = await arq.enqueue_job("digest_job", str(feed_id), days)
    if job is None:
        raise HTTPException(status_code=409, detail="a digest for this feed is already being built")
    try:
        result: dict[str, Any] = await job.result(timeout=DIGEST_TIMEOUT_S, poll_delay=0.5)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail="digest timed out (is the worker running?)") from e
    return result

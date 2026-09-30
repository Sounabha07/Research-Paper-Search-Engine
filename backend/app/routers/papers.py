"""Paper endpoints: metadata, reader content, chunks, and requesting full text."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from core.arxiv import file_stem
from core.config import get_settings
from core.db import get_engine
from core.fetchq import enqueue_fulltext
from core.redis import get_redis
from ingest.chunking import document_layout
from ingest.tei import parse_tei, read_parsed

router = APIRouter(tags=["papers"])


class FulltextResponse(BaseModel):
    arxiv_id: str
    status: str  # queued | in_progress | already_ready | abstract_only


def _pool(request: Request) -> asyncpg.Pool:
    pool: asyncpg.Pool | None = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool not ready")
    return pool


def _j(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


@router.post("/papers/{arxiv_id:path}/fulltext", response_model=FulltextResponse)
async def request_fulltext(arxiv_id: str, response: Response) -> FulltextResponse:
    """Enqueue an urgent fetch ("Get full text"). Old-style ids with a slash (math/0211159) are accepted."""
    async with get_engine().connect() as conn:
        raw = await conn.get_raw_connection()
        driver = cast(Any, raw).driver_connection
        outcome = await enqueue_fulltext(cast(asyncpg.Connection, driver), get_redis(), [arxiv_id], urgent=True)
    status = outcome[arxiv_id]
    if status == "not_found":
        raise HTTPException(status_code=404, detail=f"paper {arxiv_id} not in corpus")
    if status == "queued":
        response.status_code = 202
    return FulltextResponse(arxiv_id=arxiv_id, status=status)


@router.get("/papers/{arxiv_id:path}/content")
async def paper_content(arxiv_id: str, request: Request) -> dict[str, Any]:
    """Reader content: the canonical document text that chunk char offsets refer to, heading positions,
    and chunk spans. Abstract-only papers return just the abstract."""
    pool = _pool(request)
    paper = await pool.fetchrow("SELECT state, abstract FROM papers WHERE arxiv_id = $1", arxiv_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")
    path = get_settings().data_dir / "parsed" / f"{file_stem(arxiv_id)}.tei.xml.gz"
    if paper["state"] != "ready" or not path.exists():
        return {"full_text": False, "text": paper["abstract"], "headings": [], "chunks": []}
    tei = await asyncio.to_thread(read_parsed, path)
    text, _, heads = document_layout(parse_tei(tei))
    chunks = await pool.fetch(
        "SELECT id, section, char_start, char_end FROM chunks WHERE arxiv_id = $1 ORDER BY position", arxiv_id
    )
    return {
        "full_text": True,
        "text": text,
        "headings": [{"text": h, "start": s, "end": e} for h, s, e in heads],
        "chunks": [
            {"id": c["id"], "section": c["section"], "start": c["char_start"], "end": c["char_end"]} for c in chunks
        ],
    }


@router.get("/papers/{arxiv_id:path}")
async def get_paper(arxiv_id: str, request: Request) -> dict[str, Any]:
    row = await _pool(request).fetchrow(
        "SELECT arxiv_id, title, authors, abstract, categories, primary_category, extract(year FROM v1_date)::int"
        " AS year, v1_date, journal_ref, comments, license, badges, state, parser, fulltext_citations, meta_score,"
        " score, is_landmark FROM papers WHERE arxiv_id = $1",
        arxiv_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="paper not found")
    return {
        **dict(row),
        "authors": _j(row["authors"]),
        "badges": _j(row["badges"]),
        "full_text": row["state"] == "ready",
    }


@router.get("/chunks/{chunk_id}")
async def get_chunk(chunk_id: int, request: Request) -> dict[str, Any]:
    row = await _pool(request).fetchrow(
        "SELECT id, arxiv_id, section, position, text, token_count, char_start, char_end FROM chunks WHERE id = $1",
        chunk_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="chunk not found")
    return dict(row)

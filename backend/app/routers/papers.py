"""Paper endpoints: metadata, reader content, chunks, and requesting full text."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from agent.summary import get_summary
from core.arxiv import file_stem
from core.config import get_config, get_settings
from core.db import get_engine
from core.fetchq import enqueue_fulltext
from core.redis import get_redis
from ingest.chunking import document_layout
from ingest.tei import parse_tei, read_parsed
from retrieval.graph import paper_graph

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


@router.get("/papers/{arxiv_id:path}/summary")
async def paper_summary(arxiv_id: str, request: Request, refresh: bool = False) -> dict[str, Any]:
    """Structured summary (problem, method, datasets, results), generated with llm_light on first request."""
    summary = await get_summary(_pool(request), request.app.state.llm, get_config(), arxiv_id, refresh=refresh)
    if summary is None:
        raise HTTPException(status_code=404, detail="paper not found")
    return summary


@router.get("/papers/{arxiv_id:path}/graph")
async def graph(arxiv_id: str, request: Request, hops: int = Query(1, ge=1, le=2)) -> dict[str, Any]:
    """References and cited-by around a paper (stubs included); citations are counted within this corpus."""
    pool = _pool(request)
    if not await pool.fetchval("SELECT 1 FROM papers WHERE arxiv_id = $1", arxiv_id):
        raise HTTPException(status_code=404, detail="paper not found")
    return await paper_graph(pool, [arxiv_id], hops=hops)


@router.get("/papers/{arxiv_id:path}/references")
async def references(arxiv_id: str, request: Request) -> dict[str, Any]:
    """The paper's parsed bibliography (resolved to corpus papers or stubs where possible) and its in-corpus
    citers."""
    pool = _pool(request)
    if not await pool.fetchval("SELECT 1 FROM papers WHERE arxiv_id = $1", arxiv_id):
        raise HTTPException(status_code=404, detail="paper not found")
    refs = await pool.fetch(
        "SELECT r.position, r.title, r.year, r.raw_text, r.resolved_id, p.title AS resolved_title,"
        " extract(year FROM p.v1_date)::int AS resolved_year, p.state AS resolved_state"
        " FROM references_raw r LEFT JOIN papers p ON p.arxiv_id = r.resolved_id"
        " WHERE r.citing_id = $1 ORDER BY r.position",
        arxiv_id,
    )
    cited_by = await pool.fetch(
        "SELECT p.arxiv_id, p.title, extract(year FROM p.v1_date)::int AS year, p.fulltext_citations, p.state"
        " FROM citation_edges e JOIN papers p ON p.arxiv_id = e.citing_id WHERE e.cited_id = $1"
        " ORDER BY p.fulltext_citations DESC, p.v1_date DESC",
        arxiv_id,
    )
    return {
        "references": [
            {
                "position": r["position"],
                "title": r["title"],
                "year": r["year"],
                "raw_text": r["raw_text"],
                "resolved_id": r["resolved_id"],
                "in_corpus": r["resolved_title"] is not None,
                "resolved_title": r["resolved_title"],
                "resolved_year": r["resolved_year"],
                "full_text": r["resolved_state"] == "ready",
            }
            for r in refs
        ],
        "cited_by": [{**dict(r), "full_text": r["state"] == "ready"} for r in cited_by],
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

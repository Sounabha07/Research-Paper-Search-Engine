"""Threads and research runs (spec 12b, 14). Runs execute in the worker (`research_run_job`)."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

import asyncpg
from arq.connections import ArqRedis
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.bibtex import bibtex
from agent.run import cancel_key
from agent.threads import citation_numbers
from core.redis import get_redis, run_channel

router = APIRouter(tags=["threads"])


class RunFilters(BaseModel):
    year_from: int | None = None
    year_to: int | None = None
    categories: list[str] = Field(default_factory=list)


class NewThread(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    mode: Literal["quick", "deep"] = "quick"
    filters: RunFilters = Field(default_factory=RunFilters)
    pinned_papers: list[str] = Field(default_factory=list)
    # "paper": "Ask about this paper" - search only the pinned papers and their in-corpus references
    scope: Literal["all", "paper"] = "all"


class PinBody(BaseModel):
    arxiv_id: str


class NewRun(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    mode: Literal["quick", "deep"] = "quick"
    filters: RunFilters = Field(default_factory=RunFilters)


def _pool(request: Request) -> asyncpg.Pool:
    pool: asyncpg.Pool | None = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool not ready")
    return pool


async def _enqueue(request: Request, run_id: uuid.UUID) -> None:
    arq: ArqRedis = request.app.state.arq
    await arq.enqueue_job("research_run_job", str(run_id), _job_id=f"run:{run_id}")


@router.post("/threads")
async def create_thread(body: NewThread, request: Request) -> dict[str, str]:
    pool = _pool(request)
    filters = body.filters.model_dump()
    async with pool.acquire() as conn, conn.transaction():
        if body.pinned_papers:
            known = await conn.fetch("SELECT arxiv_id FROM papers WHERE arxiv_id = ANY($1)", body.pinned_papers)
            if len(known) != len(set(body.pinned_papers)):
                raise HTTPException(status_code=404, detail="pinned paper not in corpus")
        if body.scope == "paper":
            if not body.pinned_papers:
                raise HTTPException(status_code=422, detail="scope=paper needs pinned_papers")
            refs = await conn.fetch(
                "SELECT DISTINCT e.cited_id FROM citation_edges e JOIN papers p ON p.arxiv_id = e.cited_id"
                " WHERE e.citing_id = ANY($1)",
                body.pinned_papers,
            )
            filters["restrict_ids"] = list(dict.fromkeys([*body.pinned_papers, *(r["cited_id"] for r in refs)]))
        thread_id = await conn.fetchval(
            "INSERT INTO threads (title, pinned_papers) VALUES ($1, $2) RETURNING id",
            body.question[:200], body.pinned_papers,
        )  # fmt: skip
        run_id = await conn.fetchval(
            "INSERT INTO research_runs (thread_id, turn, question, mode, filters) VALUES ($1, 1, $2, $3, $4)"
            " RETURNING id",
            thread_id, body.question, body.mode, json.dumps(filters),
        )  # fmt: skip
    await _enqueue(request, run_id)
    return {"thread_id": str(thread_id), "run_id": str(run_id)}


@router.post("/threads/{thread_id}/runs")
async def follow_up(thread_id: uuid.UUID, body: NewRun, request: Request) -> dict[str, str]:
    pool = _pool(request)
    filters = body.filters.model_dump()
    async with pool.acquire() as conn, conn.transaction():
        if await conn.fetchval("SELECT 1 FROM threads WHERE id = $1 FOR UPDATE", thread_id) is None:
            raise HTTPException(status_code=404, detail="thread not found")
        busy = await conn.fetchval(
            "SELECT count(*) FROM research_runs WHERE thread_id = $1 AND status IN ('queued', 'running')", thread_id
        )
        if busy:
            raise HTTPException(status_code=409, detail="the previous question in this thread is still running")
        turn = await conn.fetchval(
            "SELECT coalesce(max(turn), 0) + 1 FROM research_runs WHERE thread_id = $1", thread_id
        )
        first = await conn.fetchval("SELECT filters FROM research_runs WHERE thread_id = $1 AND turn = 1", thread_id)
        restrict = (_j(first) or {}).get("restrict_ids") if first else None
        if restrict:  # a thread started with "Ask about this paper" keeps its scope
            filters["restrict_ids"] = restrict
        run_id = await conn.fetchval(
            "INSERT INTO research_runs (thread_id, turn, question, mode, filters) VALUES ($1, $2, $3, $4, $5)"
            " RETURNING id",
            thread_id, turn, body.question, body.mode, json.dumps(filters),
        )  # fmt: skip
    await _enqueue(request, run_id)
    return {"run_id": str(run_id)}


@router.get("/threads")
async def list_threads(request: Request, limit: int = 20) -> list[dict[str, Any]]:
    rows = await _pool(request).fetch(
        "SELECT t.id, t.title, t.updated_at, count(r.id) AS turns,"
        " (array_agg(r.status ORDER BY r.turn DESC))[1] AS last_status"
        " FROM threads t LEFT JOIN research_runs r ON r.thread_id = t.id"
        " GROUP BY t.id ORDER BY t.updated_at DESC LIMIT $1",
        min(max(limit, 1), 100),
    )
    return [{**dict(r), "id": str(r["id"])} for r in rows]


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: uuid.UUID, request: Request) -> dict[str, Any]:
    pool = _pool(request)
    async with pool.acquire() as conn:
        t = await conn.fetchrow("SELECT * FROM threads WHERE id = $1", thread_id)
        if t is None:
            raise HTTPException(status_code=404, detail="thread not found")
        runs = await conn.fetch(
            "SELECT id, turn, question, standalone_question, mode, status, started_at, finished_at, cost_usd"
            " FROM research_runs WHERE thread_id = $1 ORDER BY turn",
            thread_id,
        )
        numbers = await citation_numbers(conn, thread_id)
    return {
        "id": str(t["id"]), "title": t["title"], "summary": t["summary"], "pinned_papers": t["pinned_papers"],
        "created_at": t["created_at"], "updated_at": t["updated_at"],
        "runs": [{**dict(r), "id": str(r["id"])} for r in runs],
        "citations": numbers,
    }  # fmt: skip


def _j(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


@router.get("/runs/{run_id}")
async def get_run(run_id: uuid.UUID, request: Request) -> dict[str, Any]:
    pool = _pool(request)
    async with pool.acquire() as conn:
        run = await conn.fetchrow("SELECT * FROM research_runs WHERE id = $1", run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        steps = await conn.fetch(
            "SELECT seq, type, payload, created_at FROM run_steps WHERE run_id = $1 ORDER BY seq", run_id
        )
        sentences = await conn.fetch(
            "SELECT seq, section, text, verdict, sources FROM answer_sentences WHERE run_id = $1 ORDER BY seq", run_id
        )
        consensus = await conn.fetch("SELECT claim, items FROM consensus WHERE run_id = $1 ORDER BY id", run_id)
        numbers = await citation_numbers(conn, run["thread_id"])
        cited_sources: dict[str, dict[str, Any]] = {}
        sections_used: dict[str, set[str]] = {}
        for s in sentences:
            if s["verdict"] == "unsupported_removed":
                continue
            for src in _j(s["sources"]):
                cited_sources[src["id"]] = src
                sections_used.setdefault(src["arxiv_id"], set()).add(src.get("section") or "")
        chunk_ids = [int(s["chunk_id"]) for s in cited_sources.values() if s.get("chunk_id") is not None]
        chunks = {
            r["id"]: r
            for r in await conn.fetch(
                "SELECT id, text, section, char_start, char_end FROM chunks WHERE id = ANY($1::bigint[])", chunk_ids
            )
        }
        paper_ids = sorted({s["arxiv_id"] for s in cited_sources.values()})
        papers = {
            r["arxiv_id"]: r
            for r in await conn.fetch(
                "SELECT arxiv_id, title, abstract, authors, extract(year FROM v1_date)::int AS year, badges, state,"
                " primary_category FROM papers WHERE arxiv_id = ANY($1::text[])",
                paper_ids,
            )
        }
    sources = {}
    for sid, s in cited_sources.items():
        if s.get("chunk_id") is not None and int(s["chunk_id"]) in chunks:
            c = chunks[int(s["chunk_id"])]
            sources[sid] = {
                "arxiv_id": s["arxiv_id"],
                "chunk_id": c["id"],
                "type": "chunk",
                "section": c["section"],
                "text": c["text"],
                "char_start": c["char_start"],
                "char_end": c["char_end"],
            }
        elif s["arxiv_id"] in papers:
            sources[sid] = {
                "arxiv_id": s["arxiv_id"],
                "chunk_id": None,
                "type": "abstract",
                "section": "Abstract",
                "text": papers[s["arxiv_id"]]["abstract"],
                "char_start": None,
                "char_end": None,
            }
    selected = _j(run["selected_papers"]) or []
    via_chain = {p["arxiv_id"]: p.get("via_chain") for p in selected}
    started, finished = run["started_at"], run["finished_at"]
    step_list = [
        {"seq": s["seq"], "type": s["type"], "payload": _j(s["payload"]), "at": s["created_at"]} for s in steps
    ]
    sentence_list = [
        {
            "seq": s["seq"],
            "section": s["section"],
            "text": s["text"],
            "verdict": s["verdict"],
            "sources": [x["id"] for x in _j(s["sources"])],
        }
        for s in sentences
    ]
    paper_map = {
        a: {
            "number": numbers.get(a),
            "title": p["title"],
            "year": p["year"],
            "authors": _j(p["authors"]),
            "badges": _j(p["badges"]),
            "full_text": p["state"] == "ready",
            "primary_category": p["primary_category"],
            "sections_used": sorted(x for x in sections_used.get(a, set()) if x),
            "via_chain": via_chain.get(a),
        }
        for a, p in papers.items()
    }
    return {
        "id": str(run["id"]),
        "thread_id": str(run["thread_id"]),
        "turn": run["turn"],
        "question": run["question"],
        "standalone_question": run["standalone_question"],
        "mode": run["mode"],
        "status": run["status"],
        "filters": _j(run["filters"]),
        "selected_papers": selected,
        "steps": step_list,
        "sentences": sentence_list,
        "removed_count": sum(1 for s in sentences if s["verdict"] == "unsupported_removed"),
        "sources": sources,
        "papers": paper_map,
        "consensus": [{"claim": c["claim"], "items": _j(c["items"])} for c in consensus],
        "cost_usd": run["cost_usd"],
        "input_tokens": run["input_tokens"],
        "output_tokens": run["output_tokens"],
        "started_at": started,
        "finished_at": finished,
        "duration_s": (finished - started).total_seconds() if finished and started else None,
    }


@router.get("/threads/{thread_id}/bibtex")
async def thread_bibtex(thread_id: uuid.UUID, request: Request) -> Response:
    rows = await _pool(request).fetch(
        "SELECT p.arxiv_id, p.title, p.authors, extract(year FROM p.v1_date)::int AS year, p.primary_category"
        " FROM thread_citations c JOIN papers p USING (arxiv_id) WHERE c.thread_id = $1 ORDER BY c.number",
        thread_id,
    )
    papers = [{**dict(r), "authors": _j(r["authors"])} for r in rows]
    return Response(
        bibtex(papers), media_type="application/x-bibtex",
        headers={"Content-Disposition": f'attachment; filename="thread-{thread_id}.bib"'},
    )  # fmt: skip


@router.post("/threads/{thread_id}/pin")
async def pin_paper(thread_id: uuid.UUID, body: PinBody, request: Request) -> dict[str, list[str]]:
    pool = _pool(request)
    if await pool.fetchval("SELECT 1 FROM papers WHERE arxiv_id = $1", body.arxiv_id) is None:
        raise HTTPException(status_code=404, detail="paper not in corpus")
    pinned = await pool.fetchval(
        "UPDATE threads SET pinned_papers = CASE WHEN $2 = ANY(pinned_papers) THEN pinned_papers"
        " ELSE array_append(pinned_papers, $2) END WHERE id = $1 RETURNING pinned_papers",
        thread_id, body.arxiv_id,
    )  # fmt: skip
    if pinned is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return {"pinned_papers": list(pinned)}


@router.delete("/threads/{thread_id}/pin/{arxiv_id:path}")
async def unpin_paper(thread_id: uuid.UUID, arxiv_id: str, request: Request) -> dict[str, list[str]]:
    pinned = await _pool(request).fetchval(
        "UPDATE threads SET pinned_papers = array_remove(pinned_papers, $2) WHERE id = $1 RETURNING pinned_papers",
        thread_id, arxiv_id,
    )  # fmt: skip
    if pinned is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return {"pinned_papers": list(pinned)}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: uuid.UUID, request: Request) -> dict[str, str]:
    """Spec 12: a Redis flag the worker checks between steps; a queued run is cancelled right away."""
    pool = _pool(request)
    status = await pool.fetchval("SELECT status FROM research_runs WHERE id = $1", run_id)
    if status is None:
        raise HTTPException(status_code=404, detail="run not found")
    if status == "queued":
        await pool.execute("UPDATE research_runs SET status = 'cancelled', finished_at = now() WHERE id = $1", run_id)
        await get_redis().publish(run_channel(str(run_id)), json.dumps({"status": "cancelled"}))
        return {"status": "cancelled"}
    if status == "running":
        await get_redis().set(cancel_key(run_id), "1", ex=3600)
        return {"status": "cancelling"}
    return {"status": status}


FINAL = {"done", "timed_out", "failed", "cancelled"}
SSE_HEARTBEAT_S = 15.0


@router.get("/runs/{run_id}/events")
async def run_events(run_id: uuid.UUID, request: Request) -> StreamingResponse:
    """Spec 14: server-sent events for a run: steps, fetch progress, and status, until the run ends."""
    pool = _pool(request)
    status = await pool.fetchval("SELECT status FROM research_runs WHERE id = $1", run_id)
    if status is None:
        raise HTTPException(status_code=404, detail="run not found")

    async def stream() -> AsyncIterator[str]:
        pubsub = get_redis().pubsub()
        await pubsub.subscribe(run_channel(str(run_id)))
        try:
            current = await pool.fetchval("SELECT status FROM research_runs WHERE id = $1", run_id)
            yield f"data: {json.dumps({'status': current})}\n\n"
            last = time.monotonic()
            while current not in FINAL:
                if await request.is_disconnected():
                    break
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is not None:
                    yield f"data: {msg['data']}\n\n"
                    last = time.monotonic()
                    event = json.loads(msg["data"])
                    if event.get("status") in FINAL:
                        break
                elif time.monotonic() - last > SSE_HEARTBEAT_S:
                    # heartbeat, and a status check in case a message was missed
                    current = await pool.fetchval("SELECT status FROM research_runs WHERE id = $1", run_id)
                    yield f": ping\n\ndata: {json.dumps({'status': current})}\n\n"
                    last = time.monotonic()
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()  # type: ignore[no-untyped-call]

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

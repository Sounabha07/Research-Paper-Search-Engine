"""Threads and research runs (spec 12b, 14). Runs execute in the worker (`research_run_job`)."""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import asyncpg
from arq.connections import ArqRedis
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from agent.bibtex import bibtex
from agent.threads import citation_numbers

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


class NewRun(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    mode: Literal["quick", "deep"] = "quick"
    filters: RunFilters = Field(default_factory=RunFilters)


def _pool(request: Request) -> asyncpg.Pool:
    pool: asyncpg.Pool | None = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool not ready")
    return pool


def _check_mode(mode: str) -> None:
    if mode == "deep":
        raise HTTPException(status_code=400, detail="deep mode arrives in milestone 6; use mode=quick")


async def _enqueue(request: Request, run_id: uuid.UUID) -> None:
    arq: ArqRedis = request.app.state.arq
    await arq.enqueue_job("research_run_job", str(run_id), _job_id=f"run:{run_id}")


@router.post("/threads")
async def create_thread(body: NewThread, request: Request) -> dict[str, str]:
    _check_mode(body.mode)
    pool = _pool(request)
    async with pool.acquire() as conn, conn.transaction():
        thread_id = await conn.fetchval(
            "INSERT INTO threads (title, pinned_papers) VALUES ($1, $2) RETURNING id",
            body.question[:200], body.pinned_papers,
        )  # fmt: skip
        run_id = await conn.fetchval(
            "INSERT INTO research_runs (thread_id, turn, question, mode, filters) VALUES ($1, 1, $2, $3, $4)"
            " RETURNING id",
            thread_id, body.question, body.mode, body.filters.model_dump_json(),
        )  # fmt: skip
    await _enqueue(request, run_id)
    return {"thread_id": str(thread_id), "run_id": str(run_id)}


@router.post("/threads/{thread_id}/runs")
async def follow_up(thread_id: uuid.UUID, body: NewRun, request: Request) -> dict[str, str]:
    _check_mode(body.mode)
    pool = _pool(request)
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
        run_id = await conn.fetchval(
            "INSERT INTO research_runs (thread_id, turn, question, mode, filters) VALUES ($1, $2, $3, $4, $5)"
            " RETURNING id",
            thread_id, turn, body.question, body.mode, body.filters.model_dump_json(),
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

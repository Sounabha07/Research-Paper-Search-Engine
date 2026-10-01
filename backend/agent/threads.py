"""Thread state shared by the answer pipeline and the API."""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg

from agent.text import render_with_numbers

KEPT = ("supported", "partial_rewritten")


def _sources(raw: Any) -> list[dict[str, Any]]:
    return json.loads(raw) if isinstance(raw, str) else list(raw or [])


async def citation_numbers(conn: asyncpg.Connection, thread_id: uuid.UUID) -> dict[str, int]:
    rows = await conn.fetch("SELECT arxiv_id, number FROM thread_citations WHERE thread_id = $1", thread_id)
    return {r["arxiv_id"]: r["number"] for r in rows}


async def answer_markdown(
    conn: asyncpg.Connection, run_id: uuid.UUID, numbers: dict[str, int], with_numbers: bool = True
) -> str:
    """The kept sentences of a run as markdown, with tags rendered as [n] (or stripped)."""
    rows = await conn.fetch(
        "SELECT section, text, sources FROM answer_sentences WHERE run_id = $1 AND verdict = ANY($2::text[])"
        " ORDER BY seq",
        run_id,
        list(KEPT),
    )
    parts: list[str] = []
    section: str | None = None
    para: list[str] = []
    for r in rows:
        if r["section"] != section:
            if para:
                parts.append(" ".join(para))
                para = []
            section = r["section"]
            if section:
                parts.append(f"## {section}")
        tag_to_paper = {s["id"]: s["arxiv_id"] for s in _sources(r["sources"])}
        para.append(render_with_numbers(r["text"], tag_to_paper, numbers if with_numbers else {}).strip())
    if para:
        parts.append(" ".join(para))
    return "\n\n".join(parts)


async def conversation_context(
    conn: asyncpg.Connection, thread_id: uuid.UUID, before_turn: int, full_turns: int
) -> str:
    """The running summary for older turns plus the last `full_turns` turns verbatim."""
    summary = await conn.fetchval("SELECT summary FROM threads WHERE id = $1", thread_id)
    runs = await conn.fetch(
        "SELECT id, turn, question FROM research_runs WHERE thread_id = $1 AND turn < $2"
        " AND status IN ('done', 'timed_out') ORDER BY turn DESC LIMIT $3",
        thread_id,
        before_turn,
        full_turns,
    )
    if not runs and not summary:
        return ""
    numbers = await citation_numbers(conn, thread_id)
    parts = ["Conversation so far (paper numbers in [n] refer to papers cited earlier in this conversation)."]
    if summary:
        parts.append(f"Summary of earlier turns:\n{summary}")
    for r in reversed(runs):
        answer = await answer_markdown(conn, r["id"], numbers)
        parts.append(f"Turn {r['turn']} question: {r['question']}\nTurn {r['turn']} answer:\n{answer}")
    return "\n\n".join(parts)


async def thread_cited(conn: asyncpg.Connection, thread_id: uuid.UUID) -> tuple[list[str], list[int]]:
    """Papers and chunks cited by kept sentences earlier in the thread (carryover and evidence reuse)."""
    rows = await conn.fetch(
        "SELECT s.sources FROM answer_sentences s JOIN research_runs r ON r.id = s.run_id"
        " WHERE r.thread_id = $1 AND s.verdict = ANY($2::text[]) ORDER BY r.turn, s.seq",
        thread_id,
        list(KEPT),
    )
    papers: dict[str, None] = {}
    chunks: dict[int, None] = {}
    for r in rows:
        for s in _sources(r["sources"]):
            papers[s["arxiv_id"]] = None
            if s.get("chunk_id") is not None:
                chunks[int(s["chunk_id"])] = None
    return list(papers), list(chunks)

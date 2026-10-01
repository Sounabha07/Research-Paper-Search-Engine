"""Structured paper summaries.

Generated with llm_light on first request and cached. The input is the abstract plus, for full-text papers,
passages from the introduction, method, experiment, and conclusion sections. A summary made from the abstract
alone is regenerated once the paper's full text is ready.
"""

from __future__ import annotations

import json
import re
from typing import Any

import asyncpg
from pydantic import BaseModel, Field

from core.config import AppConfig
from core.llm import LLMClient

SUMMARY_MAX_TOKENS = 2048
INPUT_MAX_CHARS = 24_000  # ~6k tokens of paper text

# Section headings worth reading for a summary, in priority order; everything else fills remaining space.
_PRIORITY = [
    re.compile(p, re.I)
    for p in (
        r"introduction",
        r"conclusion|discussion|summary",
        r"method|approach|model|framework|proposed|algorithm|main result|proof",
        r"experiment|evaluation|result|benchmark|dataset|setup",
    )
]


class PaperSummary(BaseModel):
    problem: str = Field(description="the problem the paper addresses and why it matters")
    method: str = Field(description="the approach or main idea")
    datasets: list[str] = Field(default_factory=list, description="datasets or benchmarks used; empty if none")
    results: str = Field(description="main results, with numbers where the text gives them")


def _priority(section: str) -> int:
    for i, p in enumerate(_PRIORITY):
        if p.search(section or ""):
            return i
    return len(_PRIORITY)


def summary_input(title: str, abstract: str, chunks: list[tuple[str, str]], max_chars: int = INPUT_MAX_CHARS) -> str:
    """Title + abstract + chunks (section, text) chosen by section priority, kept in document order."""
    head = f"Title: {title}\n\nAbstract: {abstract}"
    budget = max_chars - len(head)
    ranked = sorted(range(len(chunks)), key=lambda i: (_priority(chunks[i][0]), i))
    chosen: list[int] = []
    for i in ranked:
        size = len(chunks[i][1]) + len(chunks[i][0]) + 4
        if size <= budget:
            chosen.append(i)
            budget -= size
    body = "\n\n".join(f"[{chunks[i][0]}]\n{chunks[i][1]}" for i in sorted(chosen))
    return f"{head}\n\n{body}" if body else head


def summary_prompt(paper_text: str) -> str:
    return f"""Summarize this research paper for a researcher deciding whether to read it.

{paper_text}

Return JSON with:
- "problem": 1-2 sentences: the problem addressed and why it matters.
- "method": 2-3 sentences: the approach or main idea (for math papers: the main result and proof technique).
- "datasets": datasets and benchmarks used for evaluation, not models or methods (empty list if none, e.g. for
  theory papers).
- "results": 1-3 sentences: the main results, with numbers where the text gives them.
Use only the text above. Do not overstate: if the paper only claims something, say so."""


async def get_summary(
    pool: asyncpg.Pool, llm: LLMClient, cfg: AppConfig, arxiv_id: str, refresh: bool = False
) -> dict[str, Any] | None:
    """Cached summary, or a new one. None if the paper is not in the corpus."""
    paper = await pool.fetchrow("SELECT title, abstract, state FROM papers WHERE arxiv_id = $1", arxiv_id)
    if paper is None:
        return None
    full_text = paper["state"] == "ready"
    cached = await pool.fetchrow("SELECT summary, created_at FROM paper_summaries WHERE arxiv_id = $1", arxiv_id)
    if cached is not None and not refresh:
        summary = json.loads(cached["summary"]) if isinstance(cached["summary"], str) else cached["summary"]
        if summary.get("source") == "full_text" or not full_text:  # upgrade abstract-only summaries
            return {**summary, "cached": True, "created_at": cached["created_at"]}

    chunks: list[tuple[str, str]] = []
    if full_text:
        rows = await pool.fetch("SELECT section, text FROM chunks WHERE arxiv_id = $1 ORDER BY position", arxiv_id)
        chunks = [(r["section"] or "", r["text"]) for r in rows]
    res = await llm.generate(
        summary_prompt(summary_input(paper["title"], paper["abstract"] or "", chunks)),
        model=cfg.models.llm_light, purpose="paper_summary", schema=PaperSummary, temperature=0.2,
        max_output_tokens=SUMMARY_MAX_TOKENS,
    )  # fmt: skip
    assert res.parsed is not None
    summary = {**res.parsed.model_dump(), "source": "full_text" if chunks else "abstract", "model": res.model}
    row = await pool.fetchrow(
        "INSERT INTO paper_summaries (arxiv_id, summary) VALUES ($1, $2) ON CONFLICT (arxiv_id)"
        " DO UPDATE SET summary = EXCLUDED.summary, created_at = now() RETURNING created_at",
        arxiv_id,
        json.dumps(summary),
    )
    return {**summary, "cached": False, "created_at": row["created_at"] if row else None}

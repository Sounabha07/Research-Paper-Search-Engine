"""Feed digests: new papers matched to a feed by embedding similarity
to its query plus keywords, summarized with llm_light.

Candidates are papers submitted in the last `feeds.digest_days`: the nearest by abstract embedding, and any whose
title or abstract contains a keyword. The best RERANK_CANDIDATES by similarity are scored with the cross-encoder
(as in search); score = rerank score + keyword_boost if a keyword matches. Kept: score at least min_score, best
top_n. (Embedding similarity alone could not separate on-topic papers from near misses.)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from pgvector import HalfVector
from pydantic import BaseModel

from core.config import AppConfig
from core.embeddings import abstract_text
from core.llm import LLMClient
from retrieval.search import SearchModels

log = logging.getLogger(__name__)

VECTOR_CANDIDATES = 200
RERANK_CANDIDATES = 60
SUMMARY_MAX_TOKENS = 4096


class DigestPaper(BaseModel):
    arxiv_id: str
    one_line: str


class DigestOut(BaseModel):
    overview: str
    papers: list[DigestPaper]


def period_of(now: datetime) -> str:
    """ISO week, e.g. 2026-W40: one digest per feed and week."""
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def matched_keywords(keywords: list[str], title: str, abstract: str) -> list[str]:
    text = f"{title} {abstract}".lower()
    return [k for k in keywords if k.strip() and k.strip().lower() in text]


def rank_candidates(
    rows: list[dict[str, Any]], keywords: list[str], min_score: float, keyword_boost: float, top_n: int
) -> list[dict[str, Any]]:
    """rows: arxiv_id, title, abstract, relevance. Returns the kept papers, best first, with score and matches."""
    out = []
    for r in rows:
        kw = matched_keywords(keywords, r["title"], r["abstract"] or "")
        score = float(r["relevance"] or 0.0) + (keyword_boost if kw else 0.0)
        if score >= min_score:  # a keyword only boosts: broad keywords alone pulled in off-topic papers
            out.append({**r, "score": round(score, 4), "keywords": kw})
    out.sort(key=lambda r: (-r["score"], r["arxiv_id"]))
    return out[:top_n]


def digest_prompt(feed_name: str, query: str, papers: list[dict[str, Any]]) -> str:
    listing = "\n\n".join(f"[{p['arxiv_id']}] {p['title']}\n{(p['abstract'] or '')[:1500]}" for p in papers)
    return f"""You write a weekly research digest for a feed named "{feed_name}" (interest: {query}).

New papers this week:
{listing}

Return JSON {{"overview": ..., "papers": [{{"arxiv_id": ..., "one_line": ...}}]}}:
- "overview": 2-4 sentences on what this week's papers are about, as they relate to the interest. Say "a paper
  proposes/reports", not that results are established: these are new preprints.
- "papers": one entry per paper above, same ids; "one_line" = one sentence saying what the paper does.
Use only the text above."""


async def ensure_query_embedding(pool: asyncpg.Pool, models: SearchModels, feed: dict[str, Any]) -> Any:
    if feed.get("query_embedding") is not None:
        return feed["query_embedding"]
    vec = HalfVector((await models.embed_queries([feed["query_text"]]))[0])
    await pool.execute("UPDATE feeds SET query_embedding = $2 WHERE id = $1", feed["id"], vec)
    return vec


async def build_digest(
    pool: asyncpg.Pool, llm: LLMClient, models: SearchModels, cfg: AppConfig, feed: dict[str, Any],
    now: datetime | None = None, days: int | None = None,
) -> dict[str, Any]:  # fmt: skip
    fc = cfg.feeds
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days or fc.digest_days)
    qvec = await ensure_query_embedding(pool, models, feed)
    keywords = [k for k in feed["keywords"] or [] if k.strip()]
    async with pool.acquire() as conn:
        await conn.execute("SET hnsw.iterative_scan = relaxed_order")
        vec_rows = await conn.fetch(
            "SELECT arxiv_id, title, abstract, 1 - (abstract_embedding <=> $1) AS similarity FROM papers"
            " WHERE v1_date >= $2 AND abstract_embedding IS NOT NULL ORDER BY abstract_embedding <=> $1 LIMIT $3",
            qvec, since, VECTOR_CANDIDATES,
        )  # fmt: skip
        kw_rows = []
        if keywords:
            kw_rows = await conn.fetch(
                "SELECT arxiv_id, title, abstract,"
                " CASE WHEN abstract_embedding IS NULL THEN 0 ELSE 1 - (abstract_embedding <=> $1) END AS similarity"
                " FROM papers WHERE v1_date >= $2"
                " AND (title ILIKE ANY($3::text[]) OR abstract ILIKE ANY($3::text[])) LIMIT $4",
                qvec, since, [f"%{k}%" for k in keywords], VECTOR_CANDIDATES,
            )  # fmt: skip
    rows = {r["arxiv_id"]: dict(r) for r in [*vec_rows, *kw_rows]}
    pool_ = sorted(rows.values(), key=lambda r: -float(r["similarity"] or 0))[:RERANK_CANDIDATES]
    scores = await models.rerank(feed["query_text"], [abstract_text(r["title"], r["abstract"] or "") for r in pool_])
    for r, s in zip(pool_, scores, strict=True):
        r["relevance"] = s
    kept = rank_candidates(pool_, keywords, fc.min_score, fc.keyword_boost, fc.top_n)
    content: dict[str, Any] = {
        "since": since.isoformat(), "generated_at": now.isoformat(), "candidates": len(rows), "overview": None,
        "model": None,
        "papers": [{"arxiv_id": p["arxiv_id"], "title": p["title"], "score": p["score"],
                    "relevance": round(float(p["relevance"]), 4), "similarity": round(float(p["similarity"] or 0), 4),
                    "keywords": p["keywords"], "one_line": None}
                   for p in kept],
    }  # fmt: skip
    if kept:
        res = await llm.generate(
            digest_prompt(feed["name"], feed["query_text"], kept), model=cfg.models.llm_light, purpose="digest",
            schema=DigestOut, temperature=0.2, max_output_tokens=SUMMARY_MAX_TOKENS,
        )  # fmt: skip
        assert res.parsed is not None
        lines = {p.arxiv_id: p.one_line for p in res.parsed.papers}
        for p in content["papers"]:
            p["one_line"] = lines.get(p["arxiv_id"])
        content["overview"] = res.parsed.overview
        content["model"] = res.model
    period = period_of(now)
    await pool.execute(
        "INSERT INTO digests (feed_id, period, content) VALUES ($1, $2, $3)"
        " ON CONFLICT (feed_id, period) DO UPDATE SET content = EXCLUDED.content, created_at = now()",
        feed["id"], period, json.dumps(content),
    )  # fmt: skip
    log.info("digest built", extra={"feed": str(feed["id"]), "period": period, "papers": len(kept)})
    return {"period": period, **content}


async def build_all(pool: asyncpg.Pool, llm: LLMClient, models: SearchModels, cfg: AppConfig) -> dict[str, int]:
    feeds = [dict(r) for r in await pool.fetch("SELECT * FROM feeds ORDER BY created_at")]
    out = {}
    for f in feeds:
        d = await build_digest(pool, llm, models, cfg, f)
        out[f["name"]] = len(d["papers"])
    return out

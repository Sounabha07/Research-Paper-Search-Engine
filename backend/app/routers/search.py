"""GET /search: paper-level hybrid search plus passages (spec 11, 14). No LLM."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from core.config import get_config
from retrieval.search import Filters, SearchModels, search

router = APIRouter(tags=["search"])


class PaperOut(BaseModel):
    arxiv_id: str
    title: str
    year: int | None
    authors: list[str]
    primary_category: str | None
    badges: dict[str, Any]
    full_text: bool
    rerank_score: float
    prior: float
    final_score: float


class PassageOut(BaseModel):
    arxiv_id: str
    source: str
    chunk_id: int | None
    section: str
    text: str
    char_start: int | None
    char_end: int | None
    rerank_score: float


class SearchOut(BaseModel):
    query: str
    papers: list[PaperOut]
    passages: list[PassageOut]
    timings_ms: dict[str, float]
    reranker_device: str
    counts: dict[str, int]


def _author_names(authors: list[dict[str, str]]) -> list[str]:
    return [" ".join(x for x in (a.get("first", ""), a.get("last", "")) if x) for a in authors]


@router.get("/search", response_model=SearchOut)
async def search_endpoint(
    request: Request,
    q: Annotated[str, Query(min_length=2, max_length=500)],
    year_from: int | None = None,
    year_to: int | None = None,
    categories: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=50)] = None,
    passages: bool = True,
) -> SearchOut:
    models: SearchModels | None = getattr(request.app.state, "search_models", None)
    pool = getattr(request.app.state, "pg_pool", None)
    if models is None or pool is None:
        raise HTTPException(status_code=503, detail="search models not loaded")
    filters = Filters(year_from=year_from, year_to=year_to, categories=tuple(categories or ()))
    async with pool.acquire() as conn:
        result = await search(conn, models, get_config(), q, filters=filters, limit=limit, with_passages=passages)
    return SearchOut(
        query=result.question,
        papers=[
            PaperOut(
                arxiv_id=p.arxiv_id, title=p.title, year=p.year, authors=_author_names(p.authors),
                primary_category=p.primary_category, badges=p.badges, full_text=p.state == "ready",
                rerank_score=round(p.rerank_score, 4), prior=round(p.prior, 4), final_score=round(p.final_score, 4),
            )
            for p in result.papers
        ],
        passages=[
            PassageOut(
                arxiv_id=x.arxiv_id, source=x.source, chunk_id=x.chunk_id, section=x.section, text=x.text,
                char_start=x.char_start, char_end=x.char_end, rerank_score=round(x.rerank_score, 4),
            )
            for x in result.passages
        ],
        timings_ms=result.timings_ms,
        reranker_device=result.reranker_device,
        counts=result.counts,
    )  # fmt: skip

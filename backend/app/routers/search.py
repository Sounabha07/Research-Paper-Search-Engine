"""GET /search: paper-level hybrid search plus passages (spec 11, 14). No LLM.

The models live only in the worker; the API enqueues an arq job and waits for its result.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from arq.connections import ArqRedis
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter(tags=["search"])

SEARCH_TIMEOUT_S = 60
RESULT_POLL_S = 0.02


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
    via_anchor: bool = False


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
    arq: ArqRedis | None = getattr(request.app.state, "arq", None)
    if arq is None:
        raise HTTPException(status_code=503, detail="job queue not connected")
    params = {
        "q": q,
        "year_from": year_from,
        "year_to": year_to,
        "categories": categories or [],
        "limit": limit,
        "passages": passages,
    }
    started = time.perf_counter()
    job = await arq.enqueue_job("search_job", params)
    if job is None:
        raise HTTPException(status_code=503, detail="could not enqueue search")
    try:
        result: dict[str, Any] = await job.result(timeout=SEARCH_TIMEOUT_S, poll_delay=RESULT_POLL_S)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail="search timed out (is the worker running?)") from e
    elapsed_ms = (time.perf_counter() - started) * 1000
    result["timings_ms"]["queue_overhead"] = round(elapsed_ms - result["timings_ms"].get("total", 0.0), 1)
    return SearchOut.model_validate(result)

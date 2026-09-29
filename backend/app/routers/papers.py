"""Paper endpoints. Milestone 3: request full text ("Get full text" button)."""

from __future__ import annotations

from typing import Any, cast

import asyncpg
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from core.db import get_engine
from core.fetchq import enqueue_fulltext
from core.redis import get_redis

router = APIRouter(prefix="/papers", tags=["papers"])


class FulltextResponse(BaseModel):
    arxiv_id: str
    status: str  # queued | in_progress | already_ready | abstract_only


@router.post("/{arxiv_id:path}/fulltext", response_model=FulltextResponse)
async def request_fulltext(arxiv_id: str, response: Response) -> FulltextResponse:
    """Enqueue an urgent fetch. Old-style ids with a slash (math/0211159) are accepted."""
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

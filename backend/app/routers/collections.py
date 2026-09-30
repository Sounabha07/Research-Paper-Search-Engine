"""Collections (spec 10, 14): named lists of papers and answers (runs), with BibTeX export."""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator

from agent.bibtex import bibtex

router = APIRouter(tags=["collections"])


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class CollectionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ItemIn(BaseModel):
    item_type: Literal["paper", "run"]
    arxiv_id: str | None = None
    run_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _one_target(self) -> ItemIn:
        if (self.item_type == "paper") != (self.arxiv_id is not None) or (self.item_type == "run") != (
            self.run_id is not None
        ):
            raise ValueError("a paper item needs arxiv_id only; a run item needs run_id only")
        return self


def _pool(request: Request) -> asyncpg.Pool:
    pool: asyncpg.Pool | None = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool not ready")
    return pool


def _j(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def bibtex_order(items: list[dict[str, Any]], run_papers: dict[uuid.UUID, list[str]]) -> list[str]:
    """Papers for a collection's BibTeX: saved papers in the order added, then the papers each saved answer
    cites, without duplicates."""
    out: list[str] = []
    for it in items:
        ids = [it["arxiv_id"]] if it["item_type"] == "paper" else run_papers.get(it["run_id"], [])
        out += [a for a in ids if a not in out]
    return out


async def _get(pool: asyncpg.Pool, collection_id: uuid.UUID) -> asyncpg.Record:
    row = await pool.fetchrow("SELECT * FROM collections WHERE id = $1", collection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="collection not found")
    return row


@router.get("/collections")
async def list_collections(
    request: Request, arxiv_id: str | None = None, run_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    """All collections with item counts. With arxiv_id or run_id, also whether each contains that item."""
    rows = await _pool(request).fetch(
        "SELECT c.id, c.name, c.description, c.updated_at, count(i.id) AS items,"
        " bool_or(i.arxiv_id = $1 OR i.run_id = $2) AS contains"
        " FROM collections c LEFT JOIN collection_items i ON i.collection_id = c.id"
        " GROUP BY c.id ORDER BY c.updated_at DESC",
        arxiv_id,
        run_id,
    )
    return [{**dict(r), "contains": bool(r["contains"])} for r in rows]


@router.post("/collections", status_code=201)
async def create_collection(body: CollectionIn, request: Request) -> dict[str, Any]:
    row = await _pool(request).fetchrow(
        "INSERT INTO collections (name, description) VALUES ($1, $2) RETURNING id, name, description, updated_at",
        body.name.strip(),
        body.description,
    )
    return dict(row) if row else {}


@router.get("/collections/{collection_id}")
async def get_collection(collection_id: uuid.UUID, request: Request) -> dict[str, Any]:
    pool = _pool(request)
    c = await _get(pool, collection_id)
    items = await pool.fetch(
        "SELECT i.id, i.item_type, i.arxiv_id, i.run_id, i.note, i.added_at,"
        " p.title, p.authors, extract(year FROM p.v1_date)::int AS year, p.badges, p.state,"
        " r.question, r.status AS run_status, r.thread_id"
        " FROM collection_items i LEFT JOIN papers p ON p.arxiv_id = i.arxiv_id"
        " LEFT JOIN research_runs r ON r.id = i.run_id WHERE i.collection_id = $1 ORDER BY i.added_at",
        collection_id,
    )
    out = [{**dict(i), "authors": _j(i["authors"]) or [], "badges": _j(i["badges"]) or {}} for i in items]
    for i in out:
        i["full_text"] = i["state"] == "ready"
    return {**dict(c), "items": out}


@router.patch("/collections/{collection_id}")
async def update_collection(collection_id: uuid.UUID, body: CollectionPatch, request: Request) -> dict[str, Any]:
    pool = _pool(request)
    await _get(pool, collection_id)
    fields = body.model_dump(exclude_unset=True)
    if fields.get("name") is not None:
        fields["name"] = fields["name"].strip()
    row = await pool.fetchrow(
        "UPDATE collections SET name = coalesce($2, name), description = CASE WHEN $4 THEN $3 ELSE description END"
        " WHERE id = $1 RETURNING id, name, description, updated_at",
        collection_id, fields.get("name"), fields.get("description"), "description" in fields,
    )  # fmt: skip
    return dict(row) if row else {}


@router.delete("/collections/{collection_id}", status_code=204)
async def delete_collection(collection_id: uuid.UUID, request: Request) -> Response:
    if await _pool(request).fetchval("DELETE FROM collections WHERE id = $1 RETURNING id", collection_id) is None:
        raise HTTPException(status_code=404, detail="collection not found")
    return Response(status_code=204)


@router.post("/collections/{collection_id}/items", status_code=201)
async def add_item(collection_id: uuid.UUID, body: ItemIn, request: Request) -> dict[str, Any]:
    pool = _pool(request)
    await _get(pool, collection_id)
    if body.arxiv_id is not None and not await pool.fetchval("SELECT 1 FROM papers WHERE arxiv_id = $1", body.arxiv_id):
        raise HTTPException(status_code=404, detail="paper not found")
    if body.run_id is not None and not await pool.fetchval("SELECT 1 FROM research_runs WHERE id = $1", body.run_id):
        raise HTTPException(status_code=404, detail="run not found")
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO collection_items (collection_id, item_type, arxiv_id, run_id, note)"
            " VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING RETURNING id",
            collection_id, body.item_type, body.arxiv_id, body.run_id, body.note,
        )  # fmt: skip
        await conn.execute("UPDATE collections SET updated_at = now() WHERE id = $1", collection_id)
    return {"id": row["id"] if row else None, "added": row is not None}


@router.delete("/collections/{collection_id}/items/{item_id}", status_code=204)
async def remove_item(collection_id: uuid.UUID, item_id: int, request: Request) -> Response:
    pool = _pool(request)
    deleted = await pool.fetchval(
        "DELETE FROM collection_items WHERE collection_id = $1 AND id = $2 RETURNING id", collection_id, item_id
    )
    if deleted is None:
        raise HTTPException(status_code=404, detail="item not found")
    await pool.execute("UPDATE collections SET updated_at = now() WHERE id = $1", collection_id)
    return Response(status_code=204)


@router.get("/collections/{collection_id}/bibtex")
async def collection_bibtex(collection_id: uuid.UUID, request: Request) -> Response:
    """BibTeX for the saved papers and for every paper cited by the saved answers."""
    pool = _pool(request)
    c = await _get(pool, collection_id)
    items = [dict(r) for r in await pool.fetch(
        "SELECT item_type, arxiv_id, run_id FROM collection_items WHERE collection_id = $1 ORDER BY added_at",
        collection_id,
    )]  # fmt: skip
    run_ids = [i["run_id"] for i in items if i["run_id"] is not None]
    run_papers: dict[uuid.UUID, list[str]] = {}
    for r in await pool.fetch(
        "SELECT s.run_id, s.seq, src->>'arxiv_id' AS arxiv_id FROM answer_sentences s,"
        " jsonb_array_elements(s.sources) src WHERE s.run_id = ANY($1) AND s.verdict <> 'unsupported_removed'"
        " ORDER BY s.run_id, s.seq",
        run_ids,
    ):
        ids = run_papers.setdefault(r["run_id"], [])
        if r["arxiv_id"] and r["arxiv_id"] not in ids:
            ids.append(r["arxiv_id"])
    order = bibtex_order(items, run_papers)
    rows = {
        r["arxiv_id"]: r
        for r in await pool.fetch(
            "SELECT arxiv_id, title, authors, extract(year FROM v1_date)::int AS year, primary_category"
            " FROM papers WHERE arxiv_id = ANY($1)",
            order,
        )
    }
    papers = [{**dict(rows[a]), "authors": _j(rows[a]["authors"])} for a in order if a in rows]
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in c["name"])[:60] or "collection"
    return Response(
        bibtex(papers), media_type="application/x-bibtex",
        headers={"Content-Disposition": f'attachment; filename="{safe}.bib"'},
    )  # fmt: skip

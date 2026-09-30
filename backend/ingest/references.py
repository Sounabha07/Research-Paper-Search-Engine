"""Reference resolution into citation edges and stubs (spec 6.5).

Order: (1) arXiv id in the reference, (2) exact normalized title, (3) trigram similarity
>= 0.9 on title plus year within 1, against corpus papers; then (4) exact title and
(5) trigram + year against `arxiv_catalog` (every arXiv record in the Kaggle snapshot), so
cited arXiv papers outside the corpus resolve without the arXiv API. The API is only used
for referenced ids that are in neither table (newer than the snapshot). Unresolved titled references become stubs once
`stub_min_corpus_citations` full-text papers cite them. Edges exist only from full-text
papers, so `fulltext_citations` = number of edges into a paper.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

import asyncpg

from ingest.tei import Reference
from ingest.text import normalize_title

log = logging.getLogger(__name__)

TRIGRAM_MIN = 0.9
YEAR_TOLERANCE = 1
# Very short normalized titles ("adam", "gshard") match unrelated papers; only ids resolve those.
MIN_TITLE_NORM_CHARS = 10
# Edge and count updates touch shared rows (popular cited papers, stubs); concurrent writers
# deadlock unless serialized. Resolution is fast, so one transaction-scoped lock is enough.
_GRAPH_LOCK_KEY = 0x63697465  # "cite"


async def lock_citation_graph(conn: asyncpg.Connection) -> None:
    """Serialize citation-graph writers for the rest of the current transaction."""
    await conn.execute("SELECT pg_advisory_xact_lock($1)", _GRAPH_LOCK_KEY)


@dataclass
class ResolutionStats:
    total: int = 0
    by_method: Counter[str] = field(default_factory=Counter)
    missing_arxiv_ids: list[str] = field(default_factory=list)  # referenced ids not in the DB yet

    @property
    def resolved(self) -> int:
        return sum(n for m, n in self.by_method.items() if m != "unresolved")

    def as_dict(self) -> dict[str, int]:
        return {"total": self.total, **self.by_method}


async def store_references(conn: asyncpg.Connection, citing_id: str, refs: list[Reference]) -> None:
    await conn.execute("DELETE FROM references_raw WHERE citing_id = $1", citing_id)
    await conn.executemany(
        "INSERT INTO references_raw (citing_id, position, title, title_norm, year, arxiv_id_found, raw_text)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7)",
        [
            (citing_id, r.position, r.title, normalize_title(r.title) if r.title else None, r.year, r.arxiv_id,
             r.raw_text)
            for r in refs
        ],
    )  # fmt: skip


def _usable_title(title_norm: str | None) -> bool:
    return bool(title_norm) and len(title_norm or "") >= MIN_TITLE_NORM_CHARS


async def _exact_matches(conn: asyncpg.Connection, title_norms: list[str]) -> dict[str, list[tuple[str, int | None]]]:
    rows = await conn.fetch(
        "SELECT title_norm, arxiv_id, extract(year FROM v1_date)::int AS year FROM papers"
        " WHERE title_norm = ANY($1::text[])",
        title_norms,
    )
    out: dict[str, list[tuple[str, int | None]]] = {}
    for r in rows:
        out.setdefault(r["title_norm"], []).append((r["arxiv_id"], r["year"]))
    return out


def _pick(candidates: list[tuple[str, int | None]], year: int | None) -> str:
    if year is None or len(candidates) == 1:
        return candidates[0][0]
    return min(candidates, key=lambda c: abs((c[1] or 0) - year))[0]


async def _trigram_match(conn: asyncpg.Connection, title_norm: str, year: int) -> str | None:
    row = await conn.fetchrow(
        "SELECT arxiv_id FROM papers"
        " WHERE title_norm % $1 AND similarity(title_norm, $1) >= $2"
        " AND abs(extract(year FROM v1_date) - $3) <= $4"
        " ORDER BY similarity(title_norm, $1) DESC LIMIT 1",
        title_norm,
        TRIGRAM_MIN,
        year,
        YEAR_TOLERANCE,
    )
    return row["arxiv_id"] if row else None


async def _catalog_exact(conn: asyncpg.Connection, title_norms: list[str]) -> dict[str, list[tuple[str, int | None]]]:
    rows = await conn.fetch(
        "SELECT title_norm, arxiv_id, year FROM arxiv_catalog WHERE title_norm = ANY($1::text[])", title_norms
    )
    out: dict[str, list[tuple[str, int | None]]] = {}
    for r in rows:
        out.setdefault(r["title_norm"], []).append((r["arxiv_id"], r["year"]))
    return out


async def _catalog_trigram(conn: asyncpg.Connection, title_norm: str, year: int) -> str | None:
    row = await conn.fetchrow(
        "SELECT arxiv_id FROM arxiv_catalog"
        " WHERE title_norm % $1 AND similarity(title_norm, $1) >= $2 AND abs(year - $3) <= $4"
        " ORDER BY similarity(title_norm, $1) DESC LIMIT 1",
        title_norm, TRIGRAM_MIN, year, YEAR_TOLERANCE,
    )  # fmt: skip
    return row["arxiv_id"] if row else None


async def resolve_for_paper(conn: asyncpg.Connection, citing_id: str, stub_min: int) -> ResolutionStats:
    """Resolve one full-text paper's references and rebuild its outgoing edges. Idempotent."""
    stats = ResolutionStats()
    refs = await conn.fetch(
        "SELECT position, title_norm, year, arxiv_id_found FROM references_raw WHERE citing_id = $1 ORDER BY position",
        citing_id,
    )
    stats.total = len(refs)
    resolved: dict[int, str] = {}
    method: dict[int, str] = {}

    # (1) arXiv ids
    for r in refs:
        if r["arxiv_id_found"] and r["arxiv_id_found"] != citing_id:
            resolved[r["position"]] = r["arxiv_id_found"]
            method[r["position"]] = "by_arxiv_id"
    ids = list({resolved[p] for p in resolved})
    if ids:
        known = {
            x["arxiv_id"]
            for x in await conn.fetch(
                "SELECT arxiv_id FROM papers WHERE arxiv_id = ANY($1)"
                " UNION SELECT arxiv_id FROM arxiv_catalog WHERE arxiv_id = ANY($1)",
                ids,
            )
        }
        stats.missing_arxiv_ids = sorted(set(ids) - known)  # only these need the arXiv API

    # (2) exact normalized title
    pending = [r for r in refs if r["position"] not in resolved and _usable_title(r["title_norm"])]
    exact = await _exact_matches(conn, list({r["title_norm"] for r in pending}))
    for r in pending:
        cands = exact.get(r["title_norm"])
        if cands:
            match = _pick(cands, r["year"])
            if match != citing_id:
                resolved[r["position"]] = match
                method[r["position"]] = "exact_title"

    # (3) trigram similarity + year
    await conn.execute(f"SET pg_trgm.similarity_threshold = {TRIGRAM_MIN}")
    for r in refs:
        if r["position"] in resolved or not _usable_title(r["title_norm"]) or r["year"] is None:
            continue
        trigram = await _trigram_match(conn, r["title_norm"], r["year"])
        if trigram and trigram != citing_id:
            resolved[r["position"]] = trigram
            method[r["position"]] = "trigram_title"

    # (4) exact title and (5) trigram + year against the full arXiv catalog
    pending = [r for r in refs if r["position"] not in resolved and _usable_title(r["title_norm"])]
    cat_exact = await _catalog_exact(conn, list({r["title_norm"] for r in pending}))
    for r in pending:
        cands = cat_exact.get(r["title_norm"])
        if cands:
            match = _pick(cands, r["year"])
            if match != citing_id:
                resolved[r["position"]] = match
                method[r["position"]] = "catalog_exact_title"
    for r in refs:
        if r["position"] in resolved or not _usable_title(r["title_norm"]) or r["year"] is None:
            continue
        cat_trigram = await _catalog_trigram(conn, r["title_norm"], r["year"])
        if cat_trigram and cat_trigram != citing_id:
            resolved[r["position"]] = cat_trigram
            method[r["position"]] = "catalog_trigram_title"

    await conn.executemany(
        "UPDATE references_raw SET resolved_id = $3 WHERE citing_id = $1 AND position = $2",
        [(citing_id, r["position"], resolved.get(r["position"])) for r in refs],
    )

    # stubs for titled references still unresolved
    unresolved_titles = {
        r["title_norm"] for r in refs if r["position"] not in resolved and _usable_title(r["title_norm"])
    }
    stub_positions = await _resolve_stubs(conn, citing_id, unresolved_titles, stub_min)
    for pos in stub_positions:
        method[pos] = "stub"
    for r in refs:
        stats.by_method[method.get(r["position"], "unresolved")] += 1

    await rebuild_edges(conn, citing_id)
    return stats


async def _resolve_stubs(conn: asyncpg.Connection, citing_id: str, title_norms: set[str], stub_min: int) -> list[int]:
    """Create or reuse stubs for titles cited by >= stub_min full-text papers. Returns this paper's positions."""
    if not title_norms:
        return []
    counts = await conn.fetch(
        "SELECT title_norm, count(DISTINCT citing_id) AS n, min(title) AS title,"
        " mode() WITHIN GROUP (ORDER BY year) AS year"
        " FROM references_raw WHERE title_norm = ANY($1::text[]) AND (resolved_id IS NULL OR resolved_id LIKE 'stub:%')"
        " GROUP BY title_norm",
        list(title_norms),
    )
    existing = {
        r["title_norm"]: r["id"]
        for r in await conn.fetch(
            "SELECT id, title_norm FROM stub_papers WHERE title_norm = ANY($1)", list(title_norms)
        )
    }
    positions: list[int] = []
    touched_citers: set[str] = set()
    for c in counts:
        stub_id = existing.get(c["title_norm"])
        if stub_id is None and c["n"] < stub_min:
            continue
        if stub_id is None:
            stub_id = await conn.fetchval(
                "INSERT INTO stub_papers (title, title_norm, year) VALUES ($1, $2, $3)"
                " ON CONFLICT (title_norm) DO UPDATE SET title = stub_papers.title RETURNING id",
                c["title"],
                c["title_norm"],
                c["year"],
            )
        rows = await conn.fetch(
            "UPDATE references_raw SET resolved_id = $2 WHERE title_norm = $1 AND resolved_id IS NULL"
            " RETURNING citing_id, position",
            c["title_norm"],
            f"stub:{stub_id}",
        )
        for row in rows:
            touched_citers.add(row["citing_id"])
            if row["citing_id"] == citing_id:
                positions.append(row["position"])
        if not rows:  # this paper's refs were already pointing at the stub
            positions += [
                r["position"]
                for r in await conn.fetch(
                    "SELECT position FROM references_raw WHERE citing_id = $1 AND title_norm = $2",
                    citing_id,
                    c["title_norm"],
                )
            ]
    for other in touched_citers - {citing_id}:
        await rebuild_edges(conn, other)
    return positions


async def rebuild_edges(conn: asyncpg.Connection, citing_id: str) -> None:
    old = {
        r["cited_id"] for r in await conn.fetch("SELECT cited_id FROM citation_edges WHERE citing_id = $1", citing_id)
    }
    await conn.execute("DELETE FROM citation_edges WHERE citing_id = $1", citing_id)
    await conn.execute(
        "INSERT INTO citation_edges (citing_id, cited_id)"
        " SELECT DISTINCT citing_id, resolved_id FROM references_raw"
        " WHERE citing_id = $1 AND resolved_id IS NOT NULL AND resolved_id <> citing_id",
        citing_id,
    )
    new = {
        r["cited_id"] for r in await conn.fetch("SELECT cited_id FROM citation_edges WHERE citing_id = $1", citing_id)
    }
    await refresh_counts(conn, old | new)


async def refresh_counts(conn: asyncpg.Connection, cited_ids: set[str]) -> None:
    papers = [c for c in cited_ids if not c.startswith("stub:")]
    stubs = [int(c.removeprefix("stub:")) for c in cited_ids if c.startswith("stub:")]
    if papers:
        await conn.execute(
            "UPDATE papers p SET fulltext_citations = c.n,"
            " badges = p.badges || jsonb_build_object('fulltext_citations', c.n)"  # the badge copy stays in step
            " FROM (SELECT a AS arxiv_id, (SELECT count(*) FROM citation_edges e WHERE e.cited_id = a) AS n"
            " FROM unnest($1::text[]) a) c WHERE p.arxiv_id = c.arxiv_id",
            papers,
        )
    if stubs:
        await conn.execute(
            "UPDATE stub_papers s SET cited_by_count ="
            " (SELECT count(*) FROM citation_edges e WHERE e.cited_id = 'stub:' || s.id)"
            " WHERE s.id = ANY($1::bigint[])",
            stubs,
        )


async def resolve_new_papers(conn: asyncpg.Connection, arxiv_ids: list[str]) -> int:
    """Papers newly added to the DB (e.g. arXiv-API metadata) may match earlier unresolved titles."""
    rows = await conn.fetch(
        "UPDATE references_raw r SET resolved_id = p.arxiv_id FROM papers p"
        " WHERE p.arxiv_id = ANY($1::text[]) AND r.resolved_id IS NULL AND r.title_norm = p.title_norm"
        " AND length(r.title_norm) >= $2 AND r.citing_id <> p.arxiv_id RETURNING r.citing_id",
        arxiv_ids,
        MIN_TITLE_NORM_CHARS,
    )
    for citing in {r["citing_id"] for r in rows}:
        await rebuild_edges(conn, citing)
    return len(rows)

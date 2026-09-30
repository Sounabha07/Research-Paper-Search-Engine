"""Citation graph around papers (spec 14 `GET /papers/{id}/graph?hops=`, spec 15 citation graph tab).

Nodes are corpus papers and stub papers (references outside the corpus, `stub:{id}`); edges run from the citing
paper to the cited one. Citation counts are counted within this corpus (fulltext_citations for papers,
cited_by_count for stubs). Stubs are never expanded: they have no references of their own.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

import asyncpg

MAX_NEIGHBORS = 40  # per node and direction, highest-cited first
MAX_NODES = 200
MAX_COMMON = 15  # run graph: references shared by at least two of the run's papers

# per-node neighbor limits, ranked by the neighbor's citation count
_REFS = """
SELECT citing_id, cited_id FROM (
    SELECT e.citing_id, e.cited_id, row_number() OVER (
        PARTITION BY e.citing_id ORDER BY coalesce(p.fulltext_citations, s.cited_by_count, 0) DESC, e.cited_id
    ) AS rn
    FROM citation_edges e
    LEFT JOIN papers p ON p.arxiv_id = e.cited_id
    LEFT JOIN stub_papers s ON e.cited_id = 'stub:' || s.id
    WHERE e.citing_id = ANY($1)
) x WHERE rn <= $2
"""
_CITED_BY = """
SELECT citing_id, cited_id FROM (
    SELECT e.citing_id, e.cited_id, row_number() OVER (
        PARTITION BY e.cited_id ORDER BY p.fulltext_citations DESC, e.citing_id
    ) AS rn
    FROM citation_edges e JOIN papers p ON p.arxiv_id = e.citing_id
    WHERE e.cited_id = ANY($1)
) x WHERE rn <= $2
"""


def is_stub(node_id: str) -> bool:
    return node_id.startswith("stub:")


def assemble(
    seeds: Sequence[str],
    edges: Iterable[tuple[str, str]],
    papers: Iterable[dict[str, Any]],
    stubs: Iterable[dict[str, Any]],
    truncated: bool = False,
) -> dict[str, Any]:
    """Graph JSON: nodes with title/year/citations, edges limited to known nodes, seeds first."""
    nodes: dict[str, dict[str, Any]] = {}
    for p in papers:
        nodes[p["arxiv_id"]] = {
            "id": p["arxiv_id"], "title": p["title"], "year": p["year"], "citations": p["fulltext_citations"] or 0,
            "full_text": p["state"] == "ready", "landmark": bool(p.get("is_landmark")), "stub": False,
        }  # fmt: skip
    for s in stubs:
        sid = f"stub:{s['id']}"
        nodes[sid] = {
            "id": sid, "title": s["title"], "year": s["year"], "citations": s["cited_by_count"] or 0,
            "full_text": False, "landmark": False, "stub": True,
        }  # fmt: skip
    seed_set = set(seeds)
    for n in nodes.values():
        n["seed"] = n["id"] in seed_set
    edge_list = sorted({(a, b) for a, b in edges if a in nodes and b in nodes and a != b})
    order = [s for s in seeds if s in nodes] + sorted(k for k in nodes if k not in seed_set)
    return {
        "nodes": [nodes[k] for k in order],
        "edges": [{"source": a, "target": b} for a, b in edge_list],
        "truncated": truncated,
    }


async def _node_rows(
    conn: asyncpg.Connection | asyncpg.Pool, ids: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    paper_ids = [i for i in ids if not is_stub(i)]
    stub_ids = [int(i[5:]) for i in ids if is_stub(i) and i[5:].isdigit()]
    papers = await conn.fetch(
        "SELECT arxiv_id, title, extract(year FROM v1_date)::int AS year, fulltext_citations, state, is_landmark"
        " FROM papers WHERE arxiv_id = ANY($1)",
        paper_ids,
    )
    stubs = await conn.fetch("SELECT id, title, year, cited_by_count FROM stub_papers WHERE id = ANY($1)", stub_ids)
    return [dict(r) for r in papers], [dict(r) for r in stubs]


async def paper_graph(
    conn: asyncpg.Connection | asyncpg.Pool, seeds: Sequence[str], hops: int = 1, max_nodes: int = MAX_NODES
) -> dict[str, Any]:
    """References and cited-by of the seeds, `hops` levels deep (stubs included, not expanded)."""
    nodes = set(seeds)
    edges: set[tuple[str, str]] = set()
    frontier = [s for s in seeds if not is_stub(s)]
    truncated = False
    for _ in range(max(0, hops)):
        if not frontier:
            break
        rows = [
            *await conn.fetch(_REFS, frontier, MAX_NEIGHBORS),
            *await conn.fetch(_CITED_BY, frontier, MAX_NEIGHBORS),
        ]
        new: list[str] = []
        for r in rows:
            a, b = r["citing_id"], r["cited_id"]
            for n in (a, b):
                if n not in nodes:
                    if len(nodes) >= max_nodes:
                        truncated = True
                        continue
                    nodes.add(n)
                    new.append(n)
            if a in nodes and b in nodes:
                edges.add((a, b))
        frontier = [n for n in new if not is_stub(n)]
    # edges among all collected nodes, not only the ones found by expansion
    corpus_nodes = [n for n in nodes if not is_stub(n)]
    rows = await conn.fetch(
        "SELECT citing_id, cited_id FROM citation_edges WHERE citing_id = ANY($1) AND cited_id = ANY($2)",
        corpus_nodes, list(nodes),
    )  # fmt: skip
    edges |= {(r["citing_id"], r["cited_id"]) for r in rows}
    papers, stubs = await _node_rows(conn, nodes)
    return assemble(seeds, edges, papers, stubs, truncated)


def common_references(edges: Iterable[tuple[str, str]], seeds: Sequence[str], limit: int = MAX_COMMON) -> list[str]:
    """Papers (or stubs) cited by at least two of the seeds, most shared first."""
    seed_set = set(seeds)
    counts = Counter(b for a, b in edges if a in seed_set and b not in seed_set)
    return [n for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if c >= 2][:limit]


async def set_graph(conn: asyncpg.Connection | asyncpg.Pool, seeds: Sequence[str]) -> dict[str, Any]:
    """Graph of a run's papers: the papers, citations among them, and references shared by two or more."""
    rows = await conn.fetch("SELECT citing_id, cited_id FROM citation_edges WHERE citing_id = ANY($1)", list(seeds))
    all_edges = [(r["citing_id"], r["cited_id"]) for r in rows]
    common = common_references(all_edges, seeds)
    nodes = set(seeds) | set(common)
    papers, stubs = await _node_rows(conn, nodes)
    return assemble(seeds, [(a, b) for a, b in all_edges if b in nodes], papers, stubs)

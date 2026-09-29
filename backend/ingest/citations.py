"""PageRank over our own citation edges (spec 9). Stubs are graph nodes but store no score."""

from __future__ import annotations

import logging
import time

import asyncpg
import numpy as np
import numpy.typing as npt
from scipy import sparse

log = logging.getLogger(__name__)

DAMPING = 0.85
TOL = 1e-10
MAX_ITER = 200


def pagerank(
    n: int, src: npt.NDArray[np.int64], dst: npt.NDArray[np.int64], damping: float = DAMPING
) -> npt.NDArray[np.float64]:
    """Power iteration; dangling nodes spread their rank uniformly. Scores sum to 1."""
    if n == 0:
        return np.zeros(0)
    out_deg = np.bincount(src, minlength=n).astype(np.float64)
    weights = 1.0 / out_deg[src]
    # m[j, i] = 1/outdeg(i) for edge i -> j
    m = sparse.csr_matrix((weights, (dst, src)), shape=(n, n))
    dangling = out_deg == 0
    rank = np.full(n, 1.0 / n)
    for _ in range(MAX_ITER):
        new = damping * (m @ rank + rank[dangling].sum() / n) + (1.0 - damping) / n
        if np.abs(new - rank).sum() < TOL:
            return np.asarray(new, dtype=np.float64)
        rank = np.asarray(new, dtype=np.float64)
    return rank


async def recompute_pagerank(conn: asyncpg.Connection) -> dict[str, float]:
    """Store PageRank for papers with at least one citation; everyone else stays NULL (no citation data)."""
    started = time.monotonic()
    edges = await conn.fetch("SELECT citing_id, cited_id FROM citation_edges")
    nodes: dict[str, int] = {}
    src, dst = [], []
    for e in edges:
        src.append(nodes.setdefault(e["citing_id"], len(nodes)))
        dst.append(nodes.setdefault(e["cited_id"], len(nodes)))
    rank = pagerank(len(nodes), np.array(src, dtype=np.int64), np.array(dst, dtype=np.int64))
    cited = set(dst)
    scores = {node: float(rank[i]) for node, i in nodes.items() if i in cited and not node.startswith("stub:")}
    await conn.execute(
        "UPDATE papers SET pagerank = NULL WHERE pagerank IS NOT NULL AND NOT (arxiv_id = ANY($1::text[]))",
        list(scores),
    )
    if scores:
        await conn.executemany(
            "UPDATE papers SET pagerank = $2 WHERE arxiv_id = $1 AND pagerank IS DISTINCT FROM $2", list(scores.items())
        )
    log.info(
        "pagerank computed",
        extra={
            "nodes": len(nodes),
            "edges": len(edges),
            "papers_scored": len(scores),
            "s": round(time.monotonic() - started, 2),
        },
    )
    return scores

"""Paper-level hybrid search over titles + abstracts (spec 11 steps 1-2). Milestone 4 adds reranking."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import asyncpg
from pgvector import HalfVector

from core.config import AppConfig
from core.embeddings import embed_query

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

_KEYWORD = """
SELECT arxiv_id FROM papers
WHERE abstract_tsv @@ websearch_to_tsquery('english', $1) AND state <> 'discovered'
ORDER BY ts_rank_cd(abstract_tsv, websearch_to_tsquery('english', $1)) DESC
LIMIT $2
"""
_VECTOR = """
SELECT arxiv_id FROM papers
WHERE state <> 'discovered'
ORDER BY abstract_embedding <=> $1
LIMIT $2
"""


def rrf_fuse(rankings: Sequence[Sequence[str]], k: int) -> list[tuple[str, float]]:
    """Reciprocal rank fusion: score(d) = sum over lists of 1 / (k + rank), rank starting at 1."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


async def search_papers(
    conn: asyncpg.Connection, embedder: SentenceTransformer, cfg: AppConfig, query: str
) -> list[tuple[str, float]]:
    r = cfg.retrieval
    keyword = [row["arxiv_id"] for row in await conn.fetch(_KEYWORD, query, r.paper_keyword_k)]
    await conn.execute(f"SET hnsw.ef_search = {int(r.hnsw_ef_search)}")
    await conn.execute("SET hnsw.iterative_scan = relaxed_order")
    vec = HalfVector(embed_query(embedder, cfg, query))
    vector = [row["arxiv_id"] for row in await conn.fetch(_VECTOR, vec, r.paper_vector_k)]
    return rrf_fuse([keyword, vector], r.rrf_k)

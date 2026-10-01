"""Metadata and citation scores, computed in SQL over the whole corpus.

Usage: python -m ingest.scores
"""

from __future__ import annotations

import asyncio
import time

from core.config import AppConfig, get_config
from core.db import raw_connection
from core.logging import setup_logging
from ingest import indexes
from ingest.citations import recompute_pagerank

# Above this many changed rows, drop and rebuild the secondary indexes around the update.
BULK_THRESHOLD = 50_000

# Papers with no citation data yet (pagerank IS NULL) use meta_score alone. Citation percentiles
# are computed only among papers that have citation data (NULLs would otherwise rank highest),
# with cume_dist so the top of a group (or its only member) gets 1.0 rather than 0.
# 'discovered' papers (metadata filled for references, not tier 1) are not scored.
_RECOMPUTE = """
WITH base AS (
    SELECT
        arxiv_id, primary_category, is_landmark, venue_accepted, code_available, pagerank,
        extract(year FROM v1_date) AS v1_year,
        CASE WHEN v1_date IS NULL THEN 0
             ELSE percent_rank() OVER (PARTITION BY primary_category ORDER BY v1_date) END AS recency,
        fulltext_citations::double precision
            / greatest(extract(epoch FROM now() - v1_date) / (86400 * 30.4375), 1) AS velocity
    FROM papers
    WHERE state <> 'discovered'
),
scored AS (
    SELECT
        arxiv_id, pagerank,
        CASE WHEN is_landmark THEN 1.0
             ELSE $1::float8 * venue_accepted::int + $2::float8 * code_available::int + $3::float8 * recency
        END AS meta_score,
        cume_dist() OVER (PARTITION BY primary_category, v1_year, pagerank IS NULL ORDER BY velocity)
            AS velocity_pct,
        cume_dist() OVER (PARTITION BY primary_category, v1_year, pagerank IS NULL ORDER BY pagerank)
            AS pagerank_pct
    FROM base
),
final AS (
    SELECT
        arxiv_id,
        meta_score,
        CASE WHEN pagerank IS NULL THEN meta_score
             ELSE 0.4 * velocity_pct + 0.3 * pagerank_pct + 0.3 * meta_score END AS score
    FROM scored
)
INSERT INTO score_new (arxiv_id, meta_score, score)
SELECT f.arxiv_id, f.meta_score, f.score
FROM final f JOIN papers p USING (arxiv_id)
WHERE p.meta_score IS DISTINCT FROM f.meta_score
   OR p.score IS DISTINCT FROM f.score
   OR p.state = 'metadata_done'
   OR NOT p.badges @> jsonb_build_object(
        'venue', p.venue_name, 'code_available', p.code_available,
        'landmark', p.is_landmark, 'fulltext_citations', p.fulltext_citations)
"""

_STAGE = """
CREATE TEMP TABLE IF NOT EXISTS score_new (arxiv_id text PRIMARY KEY, meta_score float8, score float8)
"""

_APPLY = """
UPDATE papers p SET
    meta_score = s.meta_score,
    score = s.score,
    badges = p.badges || jsonb_build_object(
        'venue', p.venue_name,
        'code_available', p.code_available,
        'landmark', p.is_landmark,
        'fulltext_citations', p.fulltext_citations
    ),
    state = CASE WHEN p.state = 'metadata_done' THEN 'signals_done' ELSE p.state END
FROM score_new s
WHERE p.arxiv_id = s.arxiv_id
"""

_SUMMARY = """
SELECT count(*) AS n,
       count(*) FILTER (WHERE is_landmark) AS landmarks,
       round(avg(meta_score)::numeric, 3) AS avg_meta,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY meta_score))::numeric, 3) AS median_meta,
       count(*) FILTER (WHERE pagerank IS NOT NULL) AS with_citations
FROM papers
"""


async def recompute_scores(cfg: AppConfig) -> str:
    """Stage the rows whose scores changed, then apply them.

    When many rows change (first run, or nightly percentile shifts) the secondary
    indexes are dropped for the update and rebuilt afterwards.
    """
    w = cfg.score_weights
    started = time.monotonic()
    conn = await raw_connection()
    dropped: list[str] = []
    rebuilt: dict[str, float] = {}
    try:
        await recompute_pagerank(conn)
        await conn.execute(_STAGE)
        await conn.execute("TRUNCATE score_new")
        await conn.execute(_RECOMPUTE, w.venue_accepted, w.code_available, w.recency)
        changed = int(await conn.fetchval("SELECT count(*) FROM score_new"))
        if changed >= BULK_THRESHOLD:
            dropped = await indexes.drop_secondary(conn)
        status = await conn.execute(_APPLY)
    finally:
        # Always, so a previously interrupted bulk run also gets its indexes back.
        rebuilt = await indexes.ensure(conn)
        if dropped:
            await conn.execute("VACUUM (ANALYZE) papers")
    try:
        s = await conn.fetchrow(_SUMMARY)
    finally:
        await conn.close()
    updated = int(status.split()[-1])
    msg = (
        f"=== Scores ===\nrows updated {updated:,} in {time.monotonic() - started:.0f}s; "
        f"papers {s['n']:,}, landmarks {s['landmarks']:,}, meta_score avg {s['avg_meta']} "
        f"median {s['median_meta']}, with citation data {s['with_citations']:,}"
    )
    if rebuilt:
        msg += f"\nbulk mode: rebuilt indexes (s): {rebuilt}"
    return msg


async def main() -> None:
    setup_logging()
    print(await recompute_scores(get_config()), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

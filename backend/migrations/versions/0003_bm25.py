"""BM25 keyword search (pg_search): BM25 indexes on papers and chunks.

The papers index also holds the filter columns (state, categories, archives, v1_date, landmark/foundational
flags) so that filters are applied inside the index and BM25 top-k stays fast. `archives` ("cs", "math") is a
generated column for the archive-level category filter.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UPGRADE = [
    "CREATE EXTENSION IF NOT EXISTS pg_search",
    """
    CREATE OR REPLACE FUNCTION category_archives(text[]) RETURNS text[]
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT coalesce(array_agg(DISTINCT split_part(c, '.', 1)), '{}') FROM unnest($1) c $$
    """,
    "ALTER TABLE papers ADD COLUMN archives text[] GENERATED ALWAYS AS (category_archives(categories)) STORED",
    # ---- BM25 indexes --------------------------------------------------------------------------
    "CREATE INDEX papers_bm25 ON papers USING bm25 (arxiv_id,"
    " (title::pdb.simple('stemmer=english', 'stopwords_language=english')),"
    " (abstract::pdb.simple('stemmer=english', 'stopwords_language=english')),"
    " (state::pdb.literal), (categories::pdb.literal), (archives::pdb.literal),"
    " v1_date, is_landmark, is_foundational) WITH (key_field='arxiv_id')",
    "CREATE INDEX chunks_bm25 ON chunks USING bm25 (id,"
    " (text::pdb.simple('stemmer=english', 'stopwords_language=english')),"
    " (arxiv_id::pdb.literal)) WITH (key_field='id')",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS chunks_bm25")
    op.execute("DROP INDEX IF EXISTS papers_bm25")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS archives")
    op.execute("DROP FUNCTION IF EXISTS category_archives(text[])")

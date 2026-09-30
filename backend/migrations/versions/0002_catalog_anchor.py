"""arXiv catalog (every record in the Kaggle snapshot, compact) and the anchor-paper partial index.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Reference resolution looks up cited arXiv papers here before calling the arXiv API;
    # the API is only needed for ids newer than the snapshot. Indexes are (re)built by the loader.
    op.execute(
        """
        CREATE TABLE arxiv_catalog (
            arxiv_id          text PRIMARY KEY,
            title             text NOT NULL,
            title_norm        text NOT NULL,
            year              smallint,
            primary_category  text
        )
        """
    )
    op.execute("CREATE INDEX arxiv_catalog_title_norm ON arxiv_catalog (title_norm)")
    op.execute("CREATE INDEX arxiv_catalog_title_norm_trgm ON arxiv_catalog USING gin (title_norm gin_trgm_ops)")
    # Anchor channel (retrieval): landmark + foundational papers only, a few hundred rows.
    op.execute("CREATE INDEX papers_anchor ON papers (arxiv_id) WHERE is_landmark OR is_foundational")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS papers_anchor")
    op.execute("DROP TABLE IF EXISTS arxiv_catalog")

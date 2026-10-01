"""Initial schema: all tables.

Revision ID: 0001
Revises:
Create Date: 2026-09-29
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

PAPER_STATES = (
    "discovered",
    "metadata_done",
    "signals_done",
    "queued_fulltext",
    "pdf_fetched",
    "parsed",
    "chunked",
    "embedded",
    "ready",
    "failed",
    "abstract_only",
)
RUN_STEP_TYPES = ("rewrite", "find_papers", "fetch", "chain", "passages", "read", "write", "verify", "consensus")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


UPGRADE = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    """
    CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
    BEGIN NEW.updated_at = now(); RETURN NEW; END;
    $$ LANGUAGE plpgsql
    """,
    # ---- corpus -------------------------------------------------------------
    f"""
    CREATE TABLE papers (
        arxiv_id            text PRIMARY KEY,
        title               text NOT NULL,
        title_norm          text NOT NULL,
        authors             jsonb NOT NULL DEFAULT '[]',
        abstract            text NOT NULL DEFAULT '',
        categories          text[] NOT NULL DEFAULT '{{}}',
        primary_category    text,
        v1_date             timestamptz,
        license             text,
        journal_ref         text,
        comments            text,
        venue_accepted      boolean NOT NULL DEFAULT false,
        venue_name          text,
        code_available      boolean NOT NULL DEFAULT false,
        is_landmark         boolean NOT NULL DEFAULT false,
        is_foundational     boolean NOT NULL DEFAULT false,
        fulltext_citations  integer NOT NULL DEFAULT 0,
        pagerank            double precision,
        meta_score          double precision,
        score               double precision,
        badges              jsonb NOT NULL DEFAULT '{{}}',
        state               text NOT NULL DEFAULT 'discovered' CHECK (state IN ({_in(PAPER_STATES)})),
        parser              text CHECK (parser IN ('grobid', 'pymupdf')),
        fail_count          integer NOT NULL DEFAULT 0,
        last_error          text,
        abstract_embedding  halfvec(384),
        abstract_tsv        tsvector GENERATED ALWAYS AS (
                                setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                                setweight(to_tsvector('english', coalesce(abstract, '')), 'B')
                            ) STORED,
        updated_at          timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE TRIGGER papers_updated_at BEFORE UPDATE ON papers FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    """
    CREATE TABLE chunks (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        arxiv_id     text NOT NULL REFERENCES papers(arxiv_id) ON DELETE CASCADE,
        section      text,
        position     integer NOT NULL,
        text         text NOT NULL,
        token_count  integer NOT NULL,
        char_start   integer NOT NULL,
        char_end     integer NOT NULL,
        embedding    halfvec(384),
        tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
        UNIQUE (arxiv_id, position)
    )
    """,
    """
    CREATE TABLE references_raw (
        citing_id       text NOT NULL REFERENCES papers(arxiv_id) ON DELETE CASCADE,
        position        integer NOT NULL,
        title           text,
        title_norm      text,
        year            integer,
        arxiv_id_found  text,
        raw_text        text,
        resolved_id     text,
        PRIMARY KEY (citing_id, position)
    )
    """,
    """
    CREATE TABLE citation_edges (
        citing_id  text NOT NULL REFERENCES papers(arxiv_id) ON DELETE CASCADE,
        cited_id   text NOT NULL,
        PRIMARY KEY (citing_id, cited_id)
    )
    """,
    """
    CREATE TABLE stub_papers (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        title           text NOT NULL,
        title_norm      text NOT NULL UNIQUE,
        year            integer,
        cited_by_count  integer NOT NULL DEFAULT 0
    )
    """,
    # ---- threads & runs -------------------------------------------------------
    """
    CREATE TABLE threads (
        id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        title          text,
        summary        text,
        pinned_papers  text[] NOT NULL DEFAULT '{}',
        created_at     timestamptz NOT NULL DEFAULT now(),
        updated_at     timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE TRIGGER threads_updated_at BEFORE UPDATE ON threads FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    """
    CREATE TABLE thread_citations (
        thread_id  uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        arxiv_id   text NOT NULL,
        number     integer NOT NULL,
        PRIMARY KEY (thread_id, arxiv_id),
        UNIQUE (thread_id, number)
    )
    """,
    """
    CREATE TABLE research_runs (
        id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        thread_id            uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        turn                 integer NOT NULL,
        question             text NOT NULL,
        standalone_question  text,
        mode                 text NOT NULL CHECK (mode IN ('quick', 'deep')),
        filters              jsonb NOT NULL DEFAULT '{}',
        status               text NOT NULL DEFAULT 'queued'
                             CHECK (status IN ('queued', 'running', 'done', 'timed_out', 'failed', 'cancelled')),
        selected_papers      jsonb,
        cost_usd             double precision NOT NULL DEFAULT 0,
        input_tokens         integer NOT NULL DEFAULT 0,
        output_tokens        integer NOT NULL DEFAULT 0,
        started_at           timestamptz,
        finished_at          timestamptz,
        UNIQUE (thread_id, turn)
    )
    """,
    f"""
    CREATE TABLE run_steps (
        id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id      uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
        seq         integer NOT NULL,
        type        text NOT NULL CHECK (type IN ({_in(RUN_STEP_TYPES)})),
        payload     jsonb NOT NULL DEFAULT '{{}}',
        created_at  timestamptz NOT NULL DEFAULT now(),
        UNIQUE (run_id, seq)
    )
    """,
    """
    CREATE TABLE evidence_notes (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id       uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
        claim        text NOT NULL,
        chunk_id     bigint REFERENCES chunks(id) ON DELETE SET NULL,
        arxiv_id     text NOT NULL,
        source_type  text NOT NULL CHECK (source_type IN ('chunk', 'abstract'))
    )
    """,
    """
    CREATE TABLE answer_sentences (
        id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id   uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
        seq      integer NOT NULL,
        section  text,
        text     text NOT NULL,
        verdict  text NOT NULL CHECK (verdict IN ('supported', 'partial_rewritten', 'unsupported_removed')),
        sources  jsonb NOT NULL DEFAULT '[]',
        UNIQUE (run_id, seq)
    )
    """,
    """
    CREATE TABLE consensus (
        id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id  uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
        claim   text NOT NULL,
        items   jsonb NOT NULL DEFAULT '[]'
    )
    """,
    """
    CREATE TABLE paper_summaries (
        arxiv_id    text PRIMARY KEY REFERENCES papers(arxiv_id) ON DELETE CASCADE,
        summary     jsonb NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    # ---- collections & feeds ----------------------------------------------------
    """
    CREATE TABLE collections (
        id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name         text NOT NULL,
        description  text,
        created_at   timestamptz NOT NULL DEFAULT now(),
        updated_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE TRIGGER collections_updated_at BEFORE UPDATE ON collections FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    """
    CREATE TABLE collection_items (
        id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        collection_id  uuid NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        item_type      text NOT NULL CHECK (item_type IN ('paper', 'run')),
        arxiv_id       text REFERENCES papers(arxiv_id) ON DELETE CASCADE,
        run_id         uuid REFERENCES research_runs(id) ON DELETE CASCADE,
        note           text,
        added_at       timestamptz NOT NULL DEFAULT now(),
        CHECK ((item_type = 'paper' AND arxiv_id IS NOT NULL AND run_id IS NULL)
            OR (item_type = 'run' AND run_id IS NOT NULL AND arxiv_id IS NULL))
    )
    """,
    "CREATE UNIQUE INDEX collection_items_paper_uq ON collection_items (collection_id, arxiv_id)"
    " WHERE arxiv_id IS NOT NULL",
    "CREATE UNIQUE INDEX collection_items_run_uq ON collection_items (collection_id, run_id) WHERE run_id IS NOT NULL",
    """
    CREATE TABLE feeds (
        id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name             text NOT NULL,
        query_text       text NOT NULL,
        query_embedding  halfvec(384),
        keywords         text[] NOT NULL DEFAULT '{}',
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE TRIGGER feeds_updated_at BEFORE UPDATE ON feeds FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    """
    CREATE TABLE digests (
        id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        feed_id     uuid NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
        period      text NOT NULL,
        content     jsonb NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now(),
        UNIQUE (feed_id, period)
    )
    """,
    # ---- llm accounting -------------------------------------------------------------
    """
    CREATE TABLE llm_calls (
        id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id         uuid REFERENCES research_runs(id) ON DELETE SET NULL,
        purpose        text NOT NULL,
        model          text NOT NULL,
        input_tokens   integer NOT NULL DEFAULT 0,
        output_tokens  integer NOT NULL DEFAULT 0,
        cost_usd       double precision,
        latency_ms     integer NOT NULL DEFAULT 0,
        cache_hit      boolean NOT NULL DEFAULT false,
        created_at     timestamptz NOT NULL DEFAULT now()
    )
    """,
    # ---- indexes (section 10) --------------------------------------------------------
    "CREATE INDEX papers_abstract_embedding_hnsw ON papers USING hnsw (abstract_embedding halfvec_cosine_ops)"
    " WITH (m = 16, ef_construction = 64)",
    "CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding halfvec_cosine_ops)"
    " WITH (m = 16, ef_construction = 64)",
    "CREATE INDEX papers_abstract_tsv_gin ON papers USING gin (abstract_tsv)",
    "CREATE INDEX chunks_tsv_gin ON chunks USING gin (tsv)",
    "CREATE INDEX papers_title_norm_trgm ON papers USING gin (title_norm gin_trgm_ops)",
    "CREATE INDEX papers_title_norm ON papers (title_norm)",
    "CREATE INDEX papers_primary_category_v1_date ON papers (primary_category, v1_date)",
    "CREATE INDEX papers_score ON papers (score)",
    "CREATE INDEX papers_state ON papers (state)",
    "CREATE INDEX chunks_arxiv_id ON chunks (arxiv_id)",
    "CREATE INDEX citation_edges_cited_id ON citation_edges (cited_id)",
    "CREATE INDEX research_runs_thread_id ON research_runs (thread_id)",
    "CREATE INDEX llm_calls_run_id ON llm_calls (run_id)",
    "CREATE INDEX llm_calls_created_at ON llm_calls (created_at)",
]

TABLES = [
    "llm_calls",
    "digests",
    "feeds",
    "collection_items",
    "collections",
    "paper_summaries",
    "consensus",
    "answer_sentences",
    "evidence_notes",
    "run_steps",
    "research_runs",
    "thread_citations",
    "threads",
    "stub_papers",
    "citation_edges",
    "references_raw",
    "chunks",
    "papers",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")

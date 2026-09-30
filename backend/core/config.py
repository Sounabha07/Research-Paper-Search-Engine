"""Configuration: tunables from config.yaml, secrets from environment variables only."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import URL


class ConfigError(RuntimeError):
    """Raised when configuration or a required environment variable is missing or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CorpusConfig(_Section):
    years_back: int
    categories_prefixes: list[str]
    landmarks_file: str
    foundational_min_fulltext_citations: int
    stub_min_corpus_citations: int
    warm_default_top_n: int


class ScoreWeights(_Section):
    venue_accepted: float
    code_available: float
    recency: float


class ChunkingConfig(_Section):
    target_tokens: int
    max_tokens: int
    overlap_tokens: int
    min_tokens: int


class ModelsConfig(_Section):
    embedding: str
    embedding_query_prefix: str
    reranker: str
    llm_main: str
    llm_light: str
    write_model: str  # model for WRITE: llm_main or llm_light
    write_thinking_level: str


class RetrievalConfig(_Section):
    paper_keyword_k: int
    paper_vector_k: int
    rerank_top_n: int
    chunk_keyword_k: int
    chunk_vector_k: int
    rrf_k: int
    rerank_min_score: float | None
    min_papers_kept: int
    name_match_boost: float
    anchor_k: int
    hnsw_ef_search: int
    prior_boost_max: float


class AgentConfig(_Section):
    max_queries: int
    papers_for_answer: int
    passages_for_answer: int
    max_passages_per_paper: int
    fetch_wait_s: int
    deep_max_fetch: int
    quick_timeout_s: int
    deep_timeout_s: int
    resume_grace_s: int
    max_notes: int
    verify_batch_size: int
    consensus_claims: int
    chain_from_top_papers: int
    chain_max_added: int
    thread_full_turns: int
    thread_carryover_boost: float


class LLMClientConfig(_Section):
    max_concurrency: int
    retries: int
    fallback_after_503_retries: int
    rpm_limits: dict[str, int] = {}
    backoff_base_s: float
    backoff_max_s: float
    cache_ttl_days: int


class ArxivConfig(_Section):
    min_interval_s: float
    user_agent: str


class ModelPrice(_Section):
    """USD per 1M tokens. None means unknown."""

    input: float | None = None
    output: float | None = None


class AppConfig(_Section):
    corpus: CorpusConfig
    score_weights: ScoreWeights
    venue_patterns: list[str]
    venue_context_words: str
    chunking: ChunkingConfig
    models: ModelsConfig
    retrieval: RetrievalConfig
    agent: AgentConfig
    llm_client: LLMClientConfig
    arxiv: ArxivConfig
    llm_prices: dict[str, ModelPrice] = Field(default_factory=dict)


_ENV_REF = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def require_env(name: str) -> str:
    """Return a non-empty environment variable or fail naming it. Never logs the value."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable {name}. Set it in .env.")
    return value


def _substitute_env(node: Any) -> Any:
    if isinstance(node, str):
        return _ENV_REF.sub(lambda m: require_env(m.group(1)), node)
    if isinstance(node, list):
        return [_substitute_env(x) for x in node]
    if isinstance(node, dict):
        return {k: _substitute_env(v) for k, v in node.items()}
    return node


def config_path() -> Path:
    return Path(os.environ.get("CONFIG_PATH", Path(__file__).resolve().parents[2] / "config.yaml"))


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    try:
        return AppConfig.model_validate(_substitute_env(raw))
    except ConfigError:
        raise
    except Exception as e:  # pydantic ValidationError
        raise ConfigError(f"Invalid config {path}: {e}") from e


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return load_config()


class Settings(BaseModel):
    """Runtime settings derived from the environment (connection strings, paths)."""

    model_config = ConfigDict(frozen=True)

    data_dir: Path
    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_db: str
    redis_url: str
    grobid_url: str

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=require_env("POSTGRES_PASSWORD"),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


DATA_SUBDIRS = ("parsed", "pdf_tmp", "models", "raw", "backups")


def ensure_data_dirs(settings: Settings | None = None) -> None:
    data_dir = (settings or get_settings()).data_dir
    for sub in DATA_SUBDIRS:
        (data_dir / sub).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env = os.environ
    return Settings(
        data_dir=Path(env.get("DATA_DIR_IN_CONTAINER", env.get("DATA_DIR", "./data"))),
        postgres_host=env.get("POSTGRES_HOST", "localhost"),
        postgres_port=int(env.get("POSTGRES_PORT", "5432")),
        postgres_user=env.get("POSTGRES_USER", "arxiv"),
        postgres_db=env.get("POSTGRES_DB", "arxiv"),
        redis_url=env.get("REDIS_URL", "redis://localhost:6379/0"),
        grobid_url=env.get("GROBID_URL", "http://localhost:8070"),
    )

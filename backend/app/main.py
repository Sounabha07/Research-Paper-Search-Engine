"""FastAPI application."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers import admin, health, papers, search
from core.config import ensure_data_dirs, get_config, get_settings
from core.db import create_pool, get_engine, get_sessionmaker
from core.embeddings import load_embedder, load_reranker
from core.llm import LLMClient, db_call_logger
from core.logging import setup_logging
from core.redis import get_redis
from retrieval.search import SearchModels

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    cfg = get_config()
    ensure_data_dirs()
    _ = get_settings().database_url  # fails early, naming POSTGRES_PASSWORD, if it is missing
    llm = LLMClient(cfg, redis=get_redis(), call_logger=db_call_logger(get_sessionmaker()))
    app.state.llm_models = await llm.check_models()
    app.state.llm = llm
    embedder, reranker = await asyncio.gather(
        asyncio.to_thread(load_embedder, cfg), asyncio.to_thread(load_reranker, cfg)
    )
    app.state.search_models = SearchModels(embedder, reranker, cfg)
    app.state.pg_pool = await create_pool(min_size=1, max_size=5)
    log.info("api started", extra={"reranker_device": app.state.search_models.reranker_device})
    yield
    await app.state.pg_pool.close()
    await get_redis().aclose()
    await get_engine().dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="arXiv Research Engine", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(papers.router)
    app.include_router(search.router)
    return app


app = create_app()

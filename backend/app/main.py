"""FastAPI application."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import create_pool as create_arq_pool
from arq.connections import RedisSettings
from fastapi import FastAPI

from app.routers import admin, health, papers, search
from core.config import ensure_data_dirs, get_config, get_settings
from core.db import get_engine, get_sessionmaker
from core.llm import LLMClient, db_call_logger
from core.logging import setup_logging
from core.redis import get_redis

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
    # Local models live only in the worker; search and answers go through the arq queue.
    app.state.arq = await create_arq_pool(RedisSettings.from_dsn(get_settings().redis_url))
    log.info("api started")
    yield
    await app.state.arq.aclose()
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

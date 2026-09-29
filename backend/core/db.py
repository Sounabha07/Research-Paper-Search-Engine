"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from core.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_size=10, max_overflow=10, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)

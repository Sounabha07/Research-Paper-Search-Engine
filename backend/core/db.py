"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from functools import lru_cache

import asyncpg
from pgvector.asyncpg import register_vector
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from core.config import get_settings, require_env


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_size=10, max_overflow=10, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def raw_connection() -> asyncpg.Connection:
    """A plain asyncpg connection for bulk COPY loads, with pgvector codecs registered."""
    s = get_settings()
    conn = await asyncpg.connect(
        host=s.postgres_host,
        port=s.postgres_port,
        user=s.postgres_user,
        password=require_env("POSTGRES_PASSWORD"),
        database=s.postgres_db,
    )
    await register_vector(conn)
    return conn


async def _init_conn(conn: asyncpg.Connection) -> None:
    await register_vector(conn)


async def create_pool(min_size: int = 1, max_size: int = 5) -> asyncpg.Pool:
    """asyncpg pool with pgvector codecs, for code that passes vectors (retrieval, pipeline)."""
    s = get_settings()
    pool = await asyncpg.create_pool(
        host=s.postgres_host, port=s.postgres_port, user=s.postgres_user, password=require_env("POSTGRES_PASSWORD"),
        database=s.postgres_db, min_size=min_size, max_size=max_size, init=_init_conn,
    )  # fmt: skip
    assert pool is not None
    return pool

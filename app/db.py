"""Async SQLAlchemy engine + session factory.

Neon note: if DATABASE_URL points at Neon's pooled endpoint (pgbouncer in
transaction mode), asyncpg's server-side prepared statement cache breaks with
DuplicatePreparedStatementError under concurrency. statement_cache_size=0
disables that cache; pool_pre_ping + pool_recycle guard against Render/Neon
idle-suspend leaving a stale connection in the pool.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Shared declarative base — imported by models.py."""


engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    pool_recycle=300,
    connect_args={
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    },
)

SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def insert_once(session: AsyncSession, row: Base) -> bool:
    """Insert a row that a unique index protects (an "already sent" or
    "already audited" marker). Returns False instead of crashing when the row
    already exists — e.g. two requests racing to send the same alert. Only
    the first one wins; the second just skips."""
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False
    return True


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — one session per request/background task."""
    async with SessionLocal() as session:
        yield session

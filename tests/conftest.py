from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401  (registers models on Base.metadata)
from app.db import Base


@pytest_asyncio.fixture
async def db_session(tmp_path):
    """A fresh SQLite-backed AsyncSession per test — file-based (not
    in-memory) so it behaves the same across multiple connections/awaits."""
    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()

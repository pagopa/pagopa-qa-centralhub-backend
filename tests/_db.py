"""Test-only database engine helpers.

Provides a NullPool-backed engine/sessionmaker for tests that hit a real
Postgres database. NullPool avoids reusing asyncpg connections across event
loops, which is required because each pytest-asyncio test runs in its own
event loop. This must NOT be used in production (see app/core/db.py, which
intentionally uses the default pooled engine).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings

test_engine = create_async_engine(settings.database_url, echo=settings.debug, poolclass=NullPool)
TestSession = async_sessionmaker(test_engine, expire_on_commit=False)


async def get_test_db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        yield session

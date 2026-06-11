from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import settings

# NullPool avoids reusing asyncpg connections across event loops, which is
# required for async test suites where each test runs in its own event loop.
engine = create_async_engine(settings.database_url, echo=settings.debug, poolclass=NullPool)
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass

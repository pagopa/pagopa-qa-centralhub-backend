"""Lock advisory di sessione non bloccanti su una connessione dedicata."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


@asynccontextmanager
async def try_advisory_lock(db: AsyncSession, key: str) -> AsyncIterator[bool]:
    """Prova `pg_try_advisory_lock(hashtext(key))`; produce se il lock è stato ottenuto.

    Una connessione propria perché la sessione ORM rilascia la sua a ogni commit, e il lock di
    sessione appartiene alla connessione. In AUTOCOMMIT, così la connessione non resta
    `idle in transaction` durante le chiamate di rete. Chiudere la connessione lo rilascia.
    """
    engine = db.bind
    assert isinstance(engine, AsyncEngine)  # noqa: S101
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        got = (
            await conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": key})
        ).scalar_one()
        try:
            yield bool(got)
        finally:
            if got:
                try:
                    await conn.execute(
                        text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": key}
                    )
                except BaseException:
                    # unlock fallito o annullato: la connessione del pool non deve tenere il lock
                    await conn.invalidate()
                    raise

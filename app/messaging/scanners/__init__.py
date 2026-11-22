"""Scanners: periodic producers of ``condition`` events.

A scanner reads the current state from the database and either publishes a condition event or
clears the condition. They run from one Celery task, each in its own transaction, and a failing
scanner never stops the others.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.messaging.scanners import nrt_pending, sanp_failing

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Scanner:
    name: str
    run: Callable[[AsyncSession], Awaitable[object]]


SCANNERS: tuple[Scanner, ...] = (
    Scanner("nrt_pending", nrt_pending.scan),
    Scanner("sanp_failing", sanp_failing.scan),
)


async def run_all(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    scanners: Sequence[Scanner] | None = None,
) -> dict[str, str]:
    """Run every scanner in its own session; return ``{name: "ok" | "error"}``."""
    if session_factory is None:
        from app.core.db import async_session

        session_factory = async_session
    results: dict[str, str] = {}
    for scanner in scanners if scanners is not None else SCANNERS:
        try:
            async with session_factory() as db:
                await scanner.run(db)
            results[scanner.name] = "ok"
        except Exception:
            logger.exception("messaging_scanner_failed", scanner=scanner.name)
            results[scanner.name] = "error"
    return results

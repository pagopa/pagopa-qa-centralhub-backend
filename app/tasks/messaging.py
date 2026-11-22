from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import structlog

from app.tasks import celery_app
from app.tasks.async_runner import run_async_task

logger = structlog.get_logger(__name__)

DISPATCH_BATCH = 50


@celery_app.task(name="app.tasks.messaging.dispatch_messages")  # type: ignore[untyped-decorator]
def dispatch_messages() -> dict[str, int]:
    return run_async_task(_dispatch())


async def _dispatch() -> dict[str, int]:
    from app.messaging.transport import get_transport

    stats = await get_transport().drain_due(limit=DISPATCH_BATCH)
    logger.info("messaging_dispatch_done", **asdict(stats))
    return asdict(stats)


@celery_app.task(name="app.tasks.messaging.purge_messages")  # type: ignore[untyped-decorator]
def purge_messages() -> dict[str, int]:
    return run_async_task(_purge())


async def _purge() -> dict[str, int]:
    from app.config import settings
    from app.core.db import async_session
    from app.messaging.retention import purge_older_than

    cutoff = datetime.now(UTC) - timedelta(days=settings.messaging_retention_days)
    async with async_session() as db:
        counts = await purge_older_than(db, cutoff)
    logger.info("messaging_purge_done", **counts)
    return counts


@celery_app.task(name="app.tasks.messaging.scan_conditions")  # type: ignore[untyped-decorator]
def scan_conditions() -> dict[str, str]:
    return run_async_task(_scan())


async def _scan() -> dict[str, str]:
    from app.messaging.scanners import run_all

    results = await run_all()
    logger.info("messaging_scan_done", **results)
    return results

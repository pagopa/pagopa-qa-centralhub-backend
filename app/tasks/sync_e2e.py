from __future__ import annotations

import asyncio

import structlog

from app.tasks import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.sync_e2e.sync_e2e_runs", bind=True, max_retries=3)
def sync_e2e_runs(self) -> dict:  # type: ignore[override]
    return asyncio.run(_async_sync())


async def _async_sync() -> dict:
    from sqlalchemy import select

    from app.core.db import async_session
    from app.models.e2e import E2eSuite
    from app.services.e2e import sync_suite

    results: dict[str, dict] = {}
    async with async_session() as db:
        suites = list(
            await db.scalars(select(E2eSuite).where(E2eSuite.enabled == True))  # noqa: E712
        )
        for suite in suites:
            try:
                count = await sync_suite(suite, db)
                logger.info("e2e_sync_ok", suite=suite.name, new_runs=count)
                results[suite.name] = {"synced": count, "error": None}
            except Exception as exc:
                logger.error("e2e_sync_error", suite=suite.name, error=str(exc))
                results[suite.name] = {"synced": 0, "error": str(exc)}
    return results

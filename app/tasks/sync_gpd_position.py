from __future__ import annotations

import asyncio

import structlog

from app.tasks import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.sync_gpd_position.sync_gpd_position_snapshots", bind=True, max_retries=3)
def sync_gpd_position_snapshots(self) -> dict:  # type: ignore[override]
    return asyncio.run(_async_sync())


async def _async_sync() -> dict:
    from app.core.db import async_session
    from app.services.gpd_position import sync_from_source

    async with async_session() as db:
        try:
            count = await sync_from_source(db)
            logger.info("gpd_position_sync_ok", item_count=count)
            return {"synced": count, "error": None}
        except Exception as exc:
            logger.error("gpd_position_sync_error", error=str(exc))
            return {"synced": 0, "error": str(exc)}

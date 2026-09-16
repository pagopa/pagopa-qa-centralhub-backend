from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.tasks import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.sync_sanp_health.sync_sanp_health")  # type: ignore[untyped-decorator]
def sync_sanp_health() -> dict[str, Any]:
    return asyncio.run(_async_sync())


async def _async_sync() -> dict[str, Any]:
    from app.core.db import async_session
    from app.services.sanp_health import sync_from_source

    async with async_session() as db:
        result = await sync_from_source(db)
        logger.info(
            "sanp_health_sync_ok",
            status=result.status,
            imported_run_count=result.imported_run_count,
        )
        return result.model_dump(mode="json")
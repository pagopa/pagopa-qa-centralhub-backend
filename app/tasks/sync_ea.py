from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog

from app.tasks import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.sync_ea.sync_ea")  # type: ignore[untyped-decorator]
def sync_ea(run_id: str | None = None) -> dict[str, Any]:
    """Weekly EA Map sync (beat, no run_id) or a run registered by ``POST /ea/sync``."""
    return asyncio.run(_async_sync(run_id))


async def _async_sync(run_id: str | None) -> dict[str, Any]:
    from app.core.db import async_session
    from app.services.ea.sync.engine import EaSyncBusyError, create_run
    from app.services.ea.sync.runner import run_with_default_sources

    if run_id is None:
        async with async_session() as db:
            try:
                run = await create_run(db, trigger="beat", actor=None)
            except EaSyncBusyError:
                logger.info("ea_sync_skipped_busy")
                return {"status": "skipped", "reason": "already running"}
            run_id = str(run.id)
    result = await run_with_default_sources(uuid.UUID(run_id))
    logger.info("ea_sync_task_done", run_id=run_id, status=result.get("status"))
    return result

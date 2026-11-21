"""Giro periodico di recupero: feature IMPLEMENTED già eseguite dagli NRT → RUNNING."""

from __future__ import annotations

from typing import Any

import structlog

from app.tasks import celery_app
from app.tasks.async_runner import run_async_task

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.sync_test_lifecycle_nrt.promote_running")  # type: ignore[untyped-decorator]
def promote_running() -> dict[str, Any]:
    return run_async_task(_async_promote())


async def _async_promote() -> dict[str, Any]:
    from app.core.db import async_session
    from app.services.test_lifecycle import nrt
    from app.services.test_lifecycle.advisory_lock import try_advisory_lock

    try:
        async with async_session() as db, try_advisory_lock(db, nrt.RUNNING_LOCK_KEY) as got:
            if not got:
                logger.info("tl_nrt_running_skipped", reason="in_progress")
                return {"status": "busy"}
            promoted = await nrt.promote_running(db)
    except Exception:
        logger.exception("tl_nrt_running_failed")
        return {"status": "error", "error": "unexpected"}
    logger.info("tl_nrt_running_finished", promoted=promoted)
    return {"status": "ok", "promoted": promoted}

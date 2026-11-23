"""Aggiornamento periodico dello stato dei task Jira collegati al Test Lifecycle."""

from __future__ import annotations

from typing import Any

import structlog

from app.tasks import celery_app
from app.tasks.async_runner import run_async_task

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.sync_test_lifecycle_jira.refresh_jira")  # type: ignore[untyped-decorator]
def refresh_jira() -> dict[str, Any]:
    return run_async_task(_async_refresh())


async def _async_refresh() -> dict[str, Any]:
    from app.core.db import async_session
    from app.services.test_lifecycle import jira_client, jira_tasks
    from app.services.test_lifecycle.advisory_lock import try_advisory_lock

    try:
        client = jira_client.get_jira_client()
    except jira_client.TlJiraNotConfigured:
        logger.info("tl_jira_refresh_skipped", reason="jira_not_configured")
        return {"status": "not_configured"}
    try:
        async with async_session() as db, try_advisory_lock(db, jira_tasks.REFRESH_LOCK_KEY) as got:
            if not got:
                logger.info("tl_jira_refresh_skipped", reason="refresh_in_progress")
                return {"status": "busy"}
            updated = await jira_tasks.refresh_all(db, client)
    except Exception:
        logger.exception("tl_jira_refresh_failed")
        return {"status": "error", "error": "unexpected"}
    logger.info("tl_jira_refresh_finished", updated=updated)
    return {"status": "ok", "updated": updated}

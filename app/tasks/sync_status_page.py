from __future__ import annotations

from typing import Any

import structlog

from app.tasks import celery_app
from app.tasks.async_runner import run_async_task

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.sync_status_page.sync_status_page")  # type: ignore[untyped-decorator]
def sync_status_page(trigger: str = "SCHEDULED", requested_by: str | None = None) -> dict[str, Any]:
    return run_async_task(_async_sync(trigger, requested_by))


async def _async_sync(trigger: str, requested_by: str | None) -> dict[str, Any]:
    from app.core.db import async_session
    from app.services.status_page_sync import run_sync

    try:
        async with async_session() as db:
            status = await run_sync(db, trigger=trigger, requested_by=requested_by)
            logger.info(
                "status_page_sync_finished",
                status=status.status,
                trigger=trigger,
                applications_checked=status.applications_checked,
                reads_failed=status.reads_failed,
            )
            return {
                "status": status.status,
                "trigger": status.trigger,
                "requested_by": status.requested_by,
                "queued_at": status.queued_at.isoformat() if status.queued_at else None,
                "started_at": status.started_at.isoformat() if status.started_at else None,
                "finished_at": status.finished_at.isoformat() if status.finished_at else None,
                "last_success_at": (
                    status.last_success_at.isoformat() if status.last_success_at else None
                ),
                "applications_checked": status.applications_checked,
                "reads_failed": status.reads_failed,
                "errors": list(status.errors or []),
            }
    finally:
        _enqueue_enrichment()


def _enqueue_enrichment() -> None:
    """Enrichment only depends on stored data, so it runs whatever the sync outcome was."""
    try:
        from app.tasks.enrich_status_page import enrich_status_page

        enrich_status_page.delay()
    except Exception:
        logger.exception("status_page_enrichment_enqueue_failed")

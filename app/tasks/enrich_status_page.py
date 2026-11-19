from __future__ import annotations

import structlog

from app.tasks import celery_app
from app.tasks.async_runner import run_async_task

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.enrich_status_page.enrich_status_page")  # type: ignore[untyped-decorator]
def enrich_status_page() -> dict[str, int]:
    return run_async_task(_async_enrich())


async def _async_enrich() -> dict[str, int]:
    from app.core.db import async_session
    from app.services.status_page_enrich import run_enrichment

    async with async_session() as db:
        counters = await run_enrichment(db)
    logger.info("status_page_enrichment_finished", **counters)
    return counters

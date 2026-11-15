from __future__ import annotations

from typing import Any

import structlog

from app.tasks import celery_app
from app.tasks.async_runner import run_async_task

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.recompute_flaky_scores.recompute_flaky_scores")  # type: ignore[untyped-decorator]
def recompute_flaky_scores() -> dict[str, Any]:
    return run_async_task(_async_recompute())


async def _async_recompute() -> dict[str, Any]:
    from app.core import db as db_module
    from app.services import flaky

    async with db_module.async_session() as db:
        try:
            status = await flaky.recompute_scores(db, trigger="SCHEDULE", requested_by=None)
        except flaky.FlakyRecomputeInProgressError:
            logger.info("flaky_recompute_skipped", reason="already running")
            return {"status": "skipped"}
        logger.info("flaky_recompute_task_ok", scored_count=status.scored_count)
        return {"status": "done", "scored_count": status.scored_count}

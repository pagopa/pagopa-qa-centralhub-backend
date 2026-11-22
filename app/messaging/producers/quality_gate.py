"""Quality Gate producer: publish a message when a Quality Gate run has been ingested.

The test-run ingestion (``POST /test-metrics/test-runs``) is a plain insert called once per run, and
TAS already fills the run's aggregates, so the totals are available at that point. The event id is
derived from the run id, so a repeated ingestion cannot produce a second event.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.messaging.bus import PublishStatus, publish, wake_dispatcher
from app.messaging.catalog import QG_RUN_COMPLETED
from app.messaging.envelope import CloudEvent, deterministic_id, new_event
from app.models.test_metrics import TestRun, TestSuite

logger = structlog.get_logger(__name__)

SOURCE = "/qa-hub/quality-gate"


def build_quality_gate_event(run: TestRun, suite: TestSuite) -> CloudEvent:
    """Pure builder. Failed means failed + broken + skipped > 0 (what TAS calls total failures)."""
    failed = run.failed_scenario or 0
    broken = run.broken_scenario or 0
    skipped = run.skipped_scenario or 0
    # Before a refresh the run still holds the raw string from the request instead of the enum.
    raw_env = getattr(run.env, "value", run.env)
    environment = str(raw_env).lower() if raw_env is not None else None
    return new_event(
        QG_RUN_COMPLETED,
        SOURCE,
        "warning" if failed + broken + skipped > 0 else "info",
        {
            "run_id": str(run.id),
            "test_object": suite.test_object,
            "test_type": suite.test_type,
            "environment": environment,
            "scenario_qty": run.scenario_qty or 0,
            "passed": run.passed_scenario or 0,
            "failed": failed,
            "broken": broken,
            "skipped": skipped,
        },
        subject=str(run.id),
        id=deterministic_id(QG_RUN_COMPLETED, str(run.id)),
        environment=environment,
    )


async def emit_quality_gate_completed(db: AsyncSession, run: TestRun) -> None:
    """Publish the run's event in the caller's transaction, then commit it with the run.

    The run (flushed, not yet committed) and its event commit together, so a failure between the
    two cannot leave a run without its message. The publish runs in a savepoint: if the hub
    rejects the event the savepoint rolls back, the run is still committed and ingestion does not
    fail because of the hub. A failure of the final commit propagates, and then neither is stored.
    """
    result = None
    try:
        suite = await db.get(TestSuite, run.suite_id)
        if suite is None:
            logger.warning("messaging_qg_suite_missing", run_id=str(run.id))
        else:
            event = build_quality_gate_event(run, suite)
            async with db.begin_nested():
                result = await publish(db, event)
    except Exception:
        logger.exception("messaging_qg_publish_failed", run_id=str(run.id))
        result = None
    await db.commit()
    if result is not None and result.status is PublishStatus.ACCEPTED:
        wake_dispatcher()

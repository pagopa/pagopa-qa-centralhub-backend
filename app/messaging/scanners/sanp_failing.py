"""SANP Health scanner: checks currently ``KO`` or ``WARNING`` in the latest complete import."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.messaging.bus import (
    PublishResult,
    PublishStatus,
    clear_condition,
    publish,
    wake_dispatcher,
)
from app.messaging.catalog import SANP_CHECKS_FAILING, SANP_FAILING_KEY
from app.messaging.envelope import new_event
from app.models.sanp_health import (
    SanpHealthResult,
    SanpHealthRun,
    SanpImportStatus,
    SanpResultStatus,
)

SOURCE = "/qa-hub/scanners/sanp"


async def scan(db: AsyncSession) -> PublishResult | None:
    """Publish the failing checks, or clear the condition when there are none.

    A member is ``<environment>:<spec_name>`` without the status, so a check flipping between
    ``WARNING`` and ``KO`` does not notify again; the message shows the current counts.
    """
    run_id = (
        await db.execute(
            select(SanpHealthRun.id)
            .where(SanpHealthRun.import_status == SanpImportStatus.COMPLETE)
            .order_by(SanpHealthRun.completed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    rows = (
        []
        if run_id is None
        else (
            await db.execute(
                select(
                    SanpHealthResult.environment,
                    SanpHealthResult.spec_name,
                    SanpHealthResult.status,
                ).where(
                    SanpHealthResult.run_id == run_id,
                    SanpHealthResult.status.in_([SanpResultStatus.KO, SanpResultStatus.WARNING]),
                )
            )
        ).all()
    )
    if not rows:
        await clear_condition(db, SANP_FAILING_KEY)
        await db.commit()
        return None
    items = sorted(f"{environment.value}:{spec_name}" for environment, spec_name, _ in rows)
    ko = sum(1 for _, _, status in rows if status == SanpResultStatus.KO)
    event = new_event(
        SANP_CHECKS_FAILING,
        SOURCE,
        "warning",
        {"ko": ko, "warning": len(rows) - ko, "items": items},
    )
    result = await publish(db, event)
    await db.commit()
    if result.status is PublishStatus.ACCEPTED:
        wake_dispatcher()
    return result

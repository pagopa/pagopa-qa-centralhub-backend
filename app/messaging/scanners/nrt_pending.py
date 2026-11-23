"""NRT scanner: runs waiting for review (``revision_result = PENDING``)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.messaging.bus import (
    PublishResult,
    PublishStatus,
    clear_condition,
    publish,
    wake_dispatcher,
)
from app.messaging.catalog import NRT_PENDING_KEY, NRT_REVIEWS_PENDING
from app.messaging.envelope import new_event
from app.services.test_metrics import list_pending_nrt_run_ids

SOURCE = "/qa-hub/scanners/nrt"


async def scan(db: AsyncSession) -> PublishResult | None:
    """Publish the current pending set, or clear the condition when nothing is pending."""
    ids = sorted(str(run_id) for run_id in await list_pending_nrt_run_ids(db))
    if not ids:
        await clear_condition(db, NRT_PENDING_KEY)
        await db.commit()
        return None
    event = new_event(NRT_REVIEWS_PENDING, SOURCE, "info", {"count": len(ids), "run_ids": ids})
    result = await publish(db, event)
    await db.commit()
    if result.status is PublishStatus.ACCEPTED:
        wake_dispatcher()
    return result

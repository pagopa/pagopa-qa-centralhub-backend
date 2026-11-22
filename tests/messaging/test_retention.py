from __future__ import annotations

# ruff: noqa: S101
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app.messaging.retention import purge_older_than
from app.models.messaging import MsgDedupe, MsgDelivery, MsgEvent
from tests._db import TestSession

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


async def test_purge_deletes_old_rows_only() -> None:
    old_id, new_id = uuid.uuid4(), uuid.uuid4()
    old_key, new_key = f"old-{uuid.uuid4()}", f"new-{uuid.uuid4()}"
    cutoff = NOW - timedelta(days=90)
    async with TestSession() as s:
        for event_id, received in [(old_id, NOW - timedelta(days=100)), (new_id, NOW)]:
            s.add(
                MsgEvent(
                    id=event_id,
                    source="/t",
                    type="t",
                    severity="info",
                    data={},
                    time=NOW,
                    received_at=received,
                )
            )
        await s.flush()
        for event_id in (old_id, new_id):
            s.add(
                MsgDelivery(event_id=event_id, consumer="c", params={}, params_hash=str(event_id))
            )
        s.add(MsgDedupe(key=old_key, members=[], updated_at=NOW - timedelta(days=100)))
        s.add(MsgDedupe(key=new_key, members=[], updated_at=NOW))
        await s.commit()

    try:
        async with TestSession() as s:
            counts = await purge_older_than(s, cutoff)
        assert counts["events"] >= 1
        assert counts["dedupe"] >= 1
        async with TestSession() as s:
            events = set((await s.execute(select(MsgEvent.id))).scalars())
            deliveries = set((await s.execute(select(MsgDelivery.event_id))).scalars())
            keys = set((await s.execute(select(MsgDedupe.key))).scalars())
        assert old_id not in events and new_id in events
        assert old_id not in deliveries and new_id in deliveries
        assert old_key not in keys and new_key in keys
    finally:
        async with TestSession() as s:
            await s.execute(delete(MsgEvent).where(MsgEvent.id.in_([old_id, new_id])))
            await s.execute(delete(MsgDedupe).where(MsgDedupe.key.in_([old_key, new_key])))
            await s.commit()

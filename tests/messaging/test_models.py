from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.models.messaging import MsgDedupe, MsgDelivery, MsgEvent, MsgInbox
from app.models.user import User
from tests._db import TestSession


@pytest.fixture
async def session() -> AsyncIterator[object]:
    async with TestSession() as s:
        yield s
        await s.rollback()


def _event() -> MsgEvent:
    return MsgEvent(
        id=uuid.uuid4(),
        source="/t",
        type="it.pagopa.qa.test",
        severity="info",
        data={"k": "v"},
        time=datetime.now(UTC),
    )


def _delivery(event_id: uuid.UUID, params_hash: str = "h") -> MsgDelivery:
    return MsgDelivery(
        event_id=event_id,
        consumer="inbox",
        params={"roles": ["qa_manager"]},
        params_hash=params_hash,
    )


async def test_delivery_defaults_and_unique_key(session) -> None:  # type: ignore[no-untyped-def]
    event = _event()
    session.add(event)
    await session.flush()
    delivery = _delivery(event.id)
    session.add(delivery)
    await session.flush()
    await session.refresh(delivery)
    assert delivery.status == "PENDING"
    assert delivery.attempts == 0
    assert delivery.lease_until is None

    session.add(_delivery(event.id))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_delivery_rejects_unknown_status(session) -> None:  # type: ignore[no-untyped-def]
    event = _event()
    session.add(event)
    await session.flush()
    bad = _delivery(event.id)
    bad.status = "X"
    session.add(bad)
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_inbox_unique_per_event_and_user_and_cascade(session) -> None:  # type: ignore[no-untyped-def]
    user = User(email=f"{uuid.uuid4()}@example.org", name="T", role="qa_manager")
    event = _event()
    session.add_all([user, event])
    await session.flush()
    session.add(
        MsgInbox(event_id=event.id, user_id=user.id, title="t", body="b", severity="info", type="x")
    )
    await session.flush()
    session.add(
        MsgInbox(
            event_id=event.id, user_id=user.id, title="t2", body="b2", severity="info", type="x"
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_deleting_event_cascades_to_deliveries_and_inbox() -> None:
    async with TestSession() as s:
        user = User(email=f"{uuid.uuid4()}@example.org", name="T", role="qa_manager")
        event = _event()
        s.add_all([user, event])
        await s.flush()
        s.add(_delivery(event.id))
        s.add(
            MsgInbox(
                event_id=event.id, user_id=user.id, title="t", body="b", severity="info", type="x"
            )
        )
        await s.commit()
        try:
            await s.execute(delete(MsgEvent).where(MsgEvent.id == event.id))
            await s.commit()
            assert (
                await s.execute(select(MsgDelivery).where(MsgDelivery.event_id == event.id))
            ).first() is None
            assert (
                await s.execute(select(MsgInbox).where(MsgInbox.event_id == event.id))
            ).first() is None
        finally:
            await s.execute(delete(User).where(User.id == user.id))
            await s.commit()


async def test_dedupe_row_roundtrip(session) -> None:  # type: ignore[no-untyped-def]
    key = f"k-{uuid.uuid4()}"
    session.add(MsgDedupe(key=key, members=["a", "b"]))
    await session.flush()
    row = (await session.execute(select(MsgDedupe).where(MsgDedupe.key == key))).scalar_one()
    assert row.members == ["a", "b"]

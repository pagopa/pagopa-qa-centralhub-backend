from __future__ import annotations

# ruff: noqa: S101
import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.messaging import rules as rules_module
from app.messaging.bus import PublishStatus, clear_condition, publish, wake_dispatcher
from app.messaging.catalog import (
    EVENT_TYPES,
    QG_RUN_COMPLETED,
    EventType,
    RenderedMessage,
    UnknownEventType,
)
from app.messaging.envelope import CloudEvent, new_event
from app.messaging.rules import DeliverySpec, Match, Rule
from app.models.messaging import MsgDedupe, MsgDelivery, MsgEvent
from tests._db import TestSession

COND = "it.pagopa.qa.test.condition.reported"


class CondData(BaseModel):
    key: str
    items: list[str]


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with TestSession() as s:
        yield s
        await s.rollback()


@pytest.fixture
def cond(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    event_type = EventType(
        type=COND,
        policy="condition",
        data_model=CondData,
        render=lambda e: RenderedMessage("t", "b", None),
        key=lambda e: str(e.data["key"]),
        members=lambda e: frozenset(e.data["items"]),
    )
    monkeypatch.setitem(EVENT_TYPES, COND, event_type)
    monkeypatch.setattr(
        rules_module,
        "RULES",
        (Rule("c", Match(type=COND), (DeliverySpec("inbox", {"roles": ["qa_manager"]}),)),),
    )
    yield


def _qg_event(**overrides: object) -> CloudEvent:
    data = {
        "run_id": "r-1",
        "test_object": "pagopa-fdr",
        "test_type": "integration",
        "environment": "uat",
        "scenario_qty": 10,
        "passed": 9,
        "failed": 1,
        "broken": 0,
        "skipped": 0,
    }
    return new_event(QG_RUN_COMPLETED, "/qa-hub/quality-gate", "warning", data, **overrides)  # type: ignore[arg-type]


def _cond_event(key: str, items: list[str]) -> CloudEvent:
    return new_event(COND, "/t", "info", {"key": key, "items": items})


async def _count_events(session: AsyncSession, event_id: str) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(MsgEvent).where(MsgEvent.id == uuid.UUID(event_id))
        )
    ).scalar_one()


async def test_publish_stores_event_and_deliveries(session: AsyncSession) -> None:
    event = _qg_event()
    result = await publish(session, event)

    assert result.status is PublishStatus.ACCEPTED
    assert result.event_id == event.id
    assert result.deliveries == 1
    rows = (
        (
            await session.execute(
                select(MsgDelivery).where(MsgDelivery.event_id == uuid.UUID(event.id))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].consumer == "inbox"
    assert rows[0].status == "PENDING"
    assert rows[0].params == {"roles": ["qa_analyst", "qa_engineer", "qa_manager"]}


async def test_publish_rolls_back_with_the_callers_transaction(session: AsyncSession) -> None:
    event = _qg_event()
    await publish(session, event)
    await session.rollback()

    assert await _count_events(session, event.id) == 0
    deliveries = (
        await session.execute(
            select(func.count())
            .select_from(MsgDelivery)
            .where(MsgDelivery.event_id == uuid.UUID(event.id))
        )
    ).scalar_one()
    assert deliveries == 0


async def test_publish_duplicate_id_is_a_noop(session: AsyncSession) -> None:
    event = _qg_event()
    first = await publish(session, event)
    second = await publish(session, event)

    assert first.status is PublishStatus.ACCEPTED
    assert second.status is PublishStatus.DUPLICATE
    assert await _count_events(session, event.id) == 1


async def test_unknown_type_and_invalid_data_raise(session: AsyncSession) -> None:
    with pytest.raises(UnknownEventType):
        await publish(session, new_event("it.pagopa.qa.nope.x.y", "/t", "info", {}))
    with pytest.raises(ValidationError):
        await publish(session, new_event(QG_RUN_COMPLETED, "/t", "info", {"run_id": "x"}))


async def test_event_without_matching_rule_is_stored_with_zero_deliveries(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rules_module, "RULES", ())
    event = _qg_event()
    result = await publish(session, event)

    assert result.status is PublishStatus.ACCEPTED
    assert result.deliveries == 0
    assert await _count_events(session, event.id) == 1


async def _stored_members(session: AsyncSession, key: str) -> list[str] | None:
    row = (
        await session.execute(
            select(MsgDedupe).where(MsgDedupe.key == key).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return None if row is None else list(row.members)


async def test_condition_first_report_is_accepted(session: AsyncSession, cond: None) -> None:
    key = f"t-{uuid.uuid4()}"
    result = await publish(session, _cond_event(key, ["b", "a"]))

    assert result.status is PublishStatus.ACCEPTED
    assert result.deliveries == 1
    assert await _stored_members(session, key) == ["a", "b"]


async def test_condition_same_set_is_suppressed(session: AsyncSession, cond: None) -> None:
    key = f"t-{uuid.uuid4()}"
    await publish(session, _cond_event(key, ["a", "b"]))
    second = _cond_event(key, ["b", "a"])
    result = await publish(session, second)

    assert result.status is PublishStatus.SUPPRESSED
    assert await _count_events(session, second.id) == 0


async def test_condition_shrunk_set_is_suppressed_and_stored(
    session: AsyncSession, cond: None
) -> None:
    key = f"t-{uuid.uuid4()}"
    await publish(session, _cond_event(key, ["a", "b"]))
    result = await publish(session, _cond_event(key, ["a"]))

    assert result.status is PublishStatus.SUPPRESSED
    assert await _stored_members(session, key) == ["a"]


async def test_condition_new_member_is_accepted_and_full_set_stored(
    session: AsyncSession, cond: None
) -> None:
    key = f"t-{uuid.uuid4()}"
    await publish(session, _cond_event(key, ["a"]))
    result = await publish(session, _cond_event(key, ["a", "b"]))

    assert result.status is PublishStatus.ACCEPTED
    assert await _stored_members(session, key) == ["a", "b"]


async def test_condition_clear_then_reappear_is_accepted_again(
    session: AsyncSession, cond: None
) -> None:
    key = f"t-{uuid.uuid4()}"
    await publish(session, _cond_event(key, ["a"]))
    await clear_condition(session, key)
    assert await _stored_members(session, key) is None

    result = await publish(session, _cond_event(key, ["a"]))
    assert result.status is PublishStatus.ACCEPTED


async def test_condition_regrow_with_a_previously_seen_member_is_accepted(
    session: AsyncSession, cond: None
) -> None:
    key = f"t-{uuid.uuid4()}"
    await publish(session, _cond_event(key, ["a", "b"]))
    shrunk = await publish(session, _cond_event(key, ["a"]))
    regrown = await publish(session, _cond_event(key, ["a", "b"]))

    assert shrunk.status is PublishStatus.SUPPRESSED
    assert regrown.status is PublishStatus.ACCEPTED


async def test_condition_concurrent_publishes_serialise(cond: None) -> None:
    key = f"t-{uuid.uuid4()}"
    events = [_cond_event(key, ["a", "b"]), _cond_event(key, ["a", "b"])]

    async def run(event: CloudEvent):  # type: ignore[no-untyped-def]
        async with TestSession() as s:
            result = await publish(s, event)
            await s.commit()
            return result

    try:
        results = await asyncio.gather(*(run(e) for e in events))
        statuses = sorted(r.status.value for r in results)
        assert statuses == ["accepted", "suppressed"]
    finally:
        async with TestSession() as s:
            await s.execute(
                delete(MsgEvent).where(MsgEvent.id.in_([uuid.UUID(e.id) for e in events]))
            )
            await s.execute(delete(MsgDedupe).where(MsgDedupe.key == key))
            await s.commit()


def test_wake_dispatcher_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise RuntimeError("broker down")

    monkeypatch.setattr("app.messaging.bus._enqueue_dispatch", boom)
    wake_dispatcher()

"""Transport contract: what every transport must satisfy (the Postgres one is the reference).

A future Azure Service Bus transport must pass the same scenarios through its own harness.
The contract only observes handler calls and ``DrainStats``, never the storage behind them.
"""

from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.messaging.consumers import CONSUMERS, Consumer, DeliveryContext
from app.messaging.envelope import CloudEvent
from app.messaging.rules import DeliverySpec
from app.messaging.transport import PostgresTransport, Transport
from app.models.messaging import MsgEvent
from tests._db import TestSession
from tests.messaging.helpers import NOW


@dataclass
class Harness:
    transport: Transport
    new_session: Callable[[], AsyncSession]
    advance: Callable[[timedelta], None]
    new_event: Callable[[AsyncSession], uuid.UUID]  # persists the event row the transport needs


class _Clock:
    def __init__(self) -> None:
        self.now: datetime = NOW

    def advance(self, delta: timedelta) -> None:
        self.now += delta


async def _new_event(session: AsyncSession) -> uuid.UUID:
    event_id = uuid.uuid4()
    session.add(
        MsgEvent(
            id=event_id, source="/t", type="it.pagopa.qa.test.thing.happened",
            severity="info", data={"k": 1}, time=NOW,
        )
    )  # fmt: skip
    await session.flush()
    return event_id


@pytest.fixture
def postgres_harness() -> Harness:
    clock = _Clock()
    return Harness(
        transport=PostgresTransport(TestSession, clock=lambda: clock.now),
        new_session=TestSession,
        advance=clock.advance,
        new_event=_new_event,  # type: ignore[arg-type]
    )


async def _enqueue(harness: Harness, params: dict[str, object]) -> uuid.UUID:
    async with harness.new_session() as s:
        event_id = await harness.new_event(s)  # type: ignore[misc]
        await harness.transport.enqueue(s, event_id, [DeliverySpec("contract", params)])
        await s.commit()
    return event_id


async def _cleanup(event_id: uuid.UUID) -> None:
    async with TestSession() as s:
        await s.execute(delete(MsgEvent).where(MsgEvent.id == event_id))
        await s.commit()


async def run_transport_contract(harness: Harness) -> None:
    calls: list[tuple[str, dict[str, object], int]] = []
    behaviour = {"fail": False}

    async def handler(event: CloudEvent, ctx: DeliveryContext) -> None:
        calls.append((event.id, ctx.params, ctx.attempt))
        if behaviour["fail"]:
            raise RuntimeError("handler failed")

    CONSUMERS["contract"] = Consumer(name="contract", handler=handler)

    # 1. a delivery reaches its handler once, with the event and its params
    event_id = await _enqueue(harness, {"roles": ["qa_manager"]})
    try:
        stats = await harness.transport.drain_due(limit=100)
        assert stats.done >= 1
        assert [c[:2] for c in calls] == [(str(event_id), {"roles": ["qa_manager"]})]
        await harness.transport.drain_due(limit=100)
        assert len(calls) == 1  # DONE deliveries are not delivered again
    finally:
        await _cleanup(event_id)

    # 2. a failing handler is retried after its backoff, not before
    calls.clear()
    behaviour["fail"] = True
    event_id = await _enqueue(harness, {})
    try:
        first = await harness.transport.drain_due(limit=100)
        assert first.failed >= 1
        assert len(calls) == 1
        await harness.transport.drain_due(limit=100)
        assert len(calls) == 1  # still backing off
        harness.advance(timedelta(seconds=61))
        behaviour["fail"] = False
        await harness.transport.drain_due(limit=100)
        assert [c[2] for c in calls] == [1, 2]
    finally:
        await _cleanup(event_id)

    # 3. after six failed attempts the delivery is dead and never delivered again
    calls.clear()
    behaviour["fail"] = True
    event_id = await _enqueue(harness, {})
    try:
        dead = 0
        for _ in range(6):
            harness.advance(timedelta(hours=7))
            dead += (await harness.transport.drain_due(limit=100)).dead
        assert len(calls) == 6
        assert dead == 1
        harness.advance(timedelta(hours=7))
        await harness.transport.drain_due(limit=100)
        assert len(calls) == 6
    finally:
        await _cleanup(event_id)


async def test_postgres_transport_satisfies_the_contract(
    postgres_harness: Harness, restore_consumers: None
) -> None:
    await run_transport_contract(postgres_harness)

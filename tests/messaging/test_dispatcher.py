from __future__ import annotations

# ruff: noqa: S101
import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.messaging import dispatcher
from app.messaging.consumers import DeliveryContext
from app.messaging.dispatcher import (
    BACKOFF_S,
    LEASE_S,
    MAX_ATTEMPTS,
    claim_due,
    record_failure,
    record_success,
)
from app.messaging.envelope import CloudEvent
from app.messaging.transport import PostgresTransport
from app.models.messaging import MsgDedupe, MsgDelivery
from tests._db import TestSession
from tests.messaging.helpers import NOW, get_delivery, register, seeded

pytestmark = pytest.mark.usefixtures("restore_consumers")


async def _claim(at):  # type: ignore[no-untyped-def]
    async with TestSession() as s:
        return await claim_due(s, limit=100, now=at)


async def test_claim_sets_lease_and_skips_leased() -> None:
    async with seeded(["c1"]) as seed:
        did = seed.delivery_ids[0]
        first = await _claim(NOW)
        assert did in {c.delivery_id for c in first}
        claimed = next(c for c in first if c.delivery_id == did)
        assert claimed.attempt == 1
        assert claimed.consumer == "c1"
        assert (await get_delivery(did)).lease_until == NOW + timedelta(seconds=LEASE_S)

        assert did not in {c.delivery_id for c in await _claim(NOW + timedelta(seconds=10))}
        assert did in {c.delivery_id for c in await _claim(NOW + timedelta(seconds=LEASE_S + 1))}


async def test_claim_skips_future_next_attempt() -> None:
    async with seeded(["c1"]) as seed:
        did = seed.delivery_ids[0]
        async with TestSession() as s:
            row = await s.get(MsgDelivery, did)
            row.next_attempt_at = NOW + timedelta(seconds=60)  # type: ignore[union-attr]
            await s.commit()
        assert did not in {c.delivery_id for c in await _claim(NOW)}
        assert did in {c.delivery_id for c in await _claim(NOW + timedelta(seconds=61))}


async def test_success_marks_done_and_clears_lease() -> None:
    async with seeded(["c1"]) as seed:
        did = seed.delivery_ids[0]
        await _claim(NOW)
        async with TestSession() as s:
            await record_success(s, did, NOW)
        row = await get_delivery(did)
        assert row.status == "DONE"
        assert row.done_at == NOW
        assert row.lease_until is None


async def test_failure_follows_backoff_then_goes_dead() -> None:
    async with seeded(["c1"]) as seed:
        did = seed.delivery_ids[0]
        for index, delay in enumerate(BACKOFF_S, start=1):
            async with TestSession() as s:
                assert await record_failure(s, did, f"boom {index}", NOW) == "PENDING"
            row = await get_delivery(did)
            assert row.attempts == index
            assert row.next_attempt_at == NOW + timedelta(seconds=delay)
            assert row.lease_until is None
        async with TestSession() as s:
            assert await record_failure(s, did, "x" * 5000, NOW) == "DEAD"
        row = await get_delivery(did)
        assert row.attempts == MAX_ATTEMPTS == 6
        assert row.status == "DEAD"
        assert row.last_error is not None
        assert len(row.last_error) == 1000


async def test_expired_lease_is_retried_after_a_crash() -> None:
    async with seeded(["c1"]) as seed:
        did = seed.delivery_ids[0]
        await _claim(NOW)  # claimed, then the worker "dies": nothing is recorded
        again = await _claim(NOW + timedelta(seconds=LEASE_S + 1))
        claimed = next(c for c in again if c.delivery_id == did)
        assert claimed.attempt == 1


async def test_failure_isolation_between_consumers() -> None:
    async def good(event: CloudEvent, ctx: DeliveryContext) -> None:
        return None

    async def bad(event: CloudEvent, ctx: DeliveryContext) -> None:
        raise RuntimeError("downstream is down")

    register("iso-good", good)
    register("iso-bad", bad)
    async with seeded(["iso-good", "iso-bad"]) as seed:
        transport = PostgresTransport(TestSession, clock=lambda: NOW)
        stats = await transport.drain_due(limit=100)

        assert stats.done >= 1
        assert stats.failed >= 1
        ok = await get_delivery(seed.delivery_ids[0])
        ko = await get_delivery(seed.delivery_ids[1])
        assert ok.status == "DONE"
        assert ko.status == "PENDING"
        assert ko.attempts == 1
        assert "downstream is down" in (ko.last_error or "")


async def test_handler_timeout_counts_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(event: CloudEvent, ctx: DeliveryContext) -> None:
        await asyncio.sleep(1)

    register("iso-slow", slow)
    monkeypatch.setattr(dispatcher, "HANDLER_TIMEOUT_S", 0.05)
    async with seeded(["iso-slow"]) as seed:
        await PostgresTransport(TestSession, clock=lambda: NOW).drain_due(limit=100)
        row = await get_delivery(seed.delivery_ids[0])
        assert row.status == "PENDING"
        assert row.attempts == 1
        assert "timeout" in (row.last_error or "").lower()


async def test_unknown_consumer_is_a_failure() -> None:
    async with seeded(["nobody-home"]) as seed:
        await PostgresTransport(TestSession, clock=lambda: NOW).drain_due(limit=100)
        row = await get_delivery(seed.delivery_ids[0])
        assert row.attempts == 1
        assert "nobody-home" in (row.last_error or "")


async def test_handler_session_is_committed_on_success_and_rolled_back_on_error() -> None:
    ok_key = f"ok-{uuid.uuid4()}"
    bad_key = f"bad-{uuid.uuid4()}"

    async def writes_ok(event: CloudEvent, ctx: DeliveryContext) -> None:
        ctx.session.add(MsgDedupe(key=ok_key, members=[]))

    async def writes_then_fails(event: CloudEvent, ctx: DeliveryContext) -> None:
        ctx.session.add(MsgDedupe(key=bad_key, members=[]))
        await ctx.session.flush()
        raise RuntimeError("after write")

    register("iso-ok", writes_ok)
    register("iso-fail", writes_then_fails)
    try:
        async with seeded(["iso-ok", "iso-fail"]):
            await PostgresTransport(TestSession, clock=lambda: NOW).drain_due(limit=100)
            async with TestSession() as s:
                keys = set((await s.execute(select(MsgDedupe.key))).scalars())
            assert ok_key in keys
            assert bad_key not in keys
    finally:
        async with TestSession() as s:
            await s.execute(delete(MsgDedupe).where(MsgDedupe.key.in_([ok_key, bad_key])))
            await s.commit()


async def test_redelivery_of_a_done_event_is_harmless_for_an_idempotent_handler() -> None:
    seen: set[str] = set()
    calls: list[str] = []

    async def idempotent(event: CloudEvent, ctx: DeliveryContext) -> None:
        calls.append(event.id)
        seen.add(event.id)

    register("iso-idem", idempotent)
    async with seeded(["iso-idem"]) as seed:
        transport = PostgresTransport(TestSession, clock=lambda: NOW)
        await transport.drain_due(limit=100)
        async with TestSession() as s:
            row = await s.get(MsgDelivery, seed.delivery_ids[0])
            row.status = "PENDING"  # type: ignore[union-attr]
            await s.commit()
        await transport.drain_due(limit=100)

    assert len(calls) == 2
    assert len(seen) == 1


async def test_a_stale_worker_cannot_overwrite_a_newer_outcome() -> None:
    async with seeded(["c1"]) as seed:
        did = seed.delivery_ids[0]
        first = next(c for c in await _claim(NOW) if c.delivery_id == did)
        # The first worker stalls; its lease expires and a second worker takes the delivery over.
        later = NOW + timedelta(seconds=LEASE_S + 1)
        second = next(c for c in await _claim(later) if c.delivery_id == did)
        assert second.lease_until != first.lease_until

        async with TestSession() as s:
            assert await record_success(s, did, later, second.lease_until) is True
        async with TestSession() as s:
            status = await record_failure(s, did, "late failure", later, first.lease_until)
        assert status == "LOST"
        row = await get_delivery(did)
        assert row.status == "DONE"
        assert row.attempts == 0
        async with TestSession() as s:
            assert await record_success(s, did, later, first.lease_until) is False


async def test_the_lease_is_renewed_before_each_handler_call_in_a_batch() -> None:
    state = {"now": NOW}
    stolen: list[uuid.UUID] = []

    async def slow_first(event: CloudEvent, ctx: DeliveryContext) -> None:
        state["now"] += timedelta(seconds=LEASE_S + 50)  # longer than the batch lease

    async def check_second(event: CloudEvent, ctx: DeliveryContext) -> None:
        async with TestSession() as s:
            stolen.extend(c.delivery_id for c in await claim_due(s, limit=100, now=state["now"]))

    register("lease-first", slow_first)
    register("lease-second", check_second)
    async with seeded(["lease-first", "lease-second"]) as seed:
        await PostgresTransport(TestSession, clock=lambda: state["now"]).drain_due(limit=100)
        assert seed.delivery_ids[1] not in stolen
        assert (await get_delivery(seed.delivery_ids[1])).status == "DONE"

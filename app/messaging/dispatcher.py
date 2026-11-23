"""Delivery bookkeeping for the Postgres transport: claim with a lease, record the outcome.

No row lock or transaction is held while a handler runs (handlers may call the network):
``claim_due`` takes a lease and commits, the handler runs outside any transaction, then
``record_success`` or ``record_failure`` writes the result. A worker that dies mid-handler leaves
the lease to expire, after which the delivery is claimed again.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.messaging import MsgDelivery

BACKOFF_S = (60, 300, 1800, 7200, 21600)
MAX_ATTEMPTS = 6
LEASE_S = 300
HANDLER_TIMEOUT_S: float = 60
ERROR_MAX_LEN = 1000


@dataclass(frozen=True)
class Claimed:
    delivery_id: uuid.UUID
    event_id: uuid.UUID
    consumer: str
    params: dict[str, Any]
    attempt: int
    lease_until: datetime  # the lease this worker holds; it also proves ownership when recording


async def claim_due(session: AsyncSession, *, limit: int, now: datetime) -> list[Claimed]:
    """Lease up to ``limit`` due deliveries and commit; several workers may call this at once."""
    rows = (
        (
            await session.execute(
                select(MsgDelivery)
                .where(
                    MsgDelivery.status == "PENDING",
                    MsgDelivery.next_attempt_at <= now,
                    or_(MsgDelivery.lease_until.is_(None), MsgDelivery.lease_until < now),
                )
                .order_by(MsgDelivery.next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    lease_until = now + timedelta(seconds=LEASE_S)
    claimed: list[Claimed] = []
    for row in rows:
        row.lease_until = lease_until
        claimed.append(
            Claimed(
                row.id, row.event_id, row.consumer, dict(row.params), row.attempts + 1, lease_until
            )
        )
    await session.commit()
    return claimed


async def renew_lease(
    session: AsyncSession, delivery_id: uuid.UUID, held: datetime, now: datetime
) -> datetime | None:
    """Extend the lease this worker holds before running the next handler of a batch.

    Returns the new lease, or None when the delivery is no longer ours (another worker claimed it
    after our lease lapsed, or it is already settled); the caller must then skip it.
    """
    new_lease = now + timedelta(seconds=LEASE_S)
    result = await session.execute(
        update(MsgDelivery)
        .where(
            MsgDelivery.id == delivery_id,
            MsgDelivery.status == "PENDING",
            MsgDelivery.lease_until == held,
        )
        .values(lease_until=new_lease)
    )
    await session.commit()
    return new_lease if result.rowcount else None  # type: ignore[attr-defined]


async def _owned(
    session: AsyncSession, delivery_id: uuid.UUID, lease_until: datetime | None
) -> MsgDelivery | None:
    """Lock the delivery; with ``lease_until`` only when it is still PENDING under that lease."""
    stmt = select(MsgDelivery).where(MsgDelivery.id == delivery_id).with_for_update()
    if lease_until is not None:
        stmt = stmt.where(MsgDelivery.status == "PENDING", MsgDelivery.lease_until == lease_until)
    return (
        await session.execute(stmt.execution_options(populate_existing=True))
    ).scalar_one_or_none()


async def record_success(
    session: AsyncSession,
    delivery_id: uuid.UUID,
    now: datetime,
    lease_until: datetime | None = None,
) -> bool:
    """Mark the delivery done; False (nothing written) when the lease is no longer ours."""
    row = await _owned(session, delivery_id, lease_until)
    if row is None:
        await session.rollback()
        return False
    row.status = "DONE"
    row.done_at = now
    row.lease_until = None
    await session.commit()
    return True


async def record_failure(
    session: AsyncSession,
    delivery_id: uuid.UUID,
    error: str,
    now: datetime,
    lease_until: datetime | None = None,
) -> str:
    """Record a failed attempt; return ``PENDING``, ``DEAD`` or ``LOST`` (lease not ours)."""
    row = await _owned(session, delivery_id, lease_until)
    if row is None:
        await session.rollback()
        return "LOST"
    row.attempts += 1
    row.last_error = error[:ERROR_MAX_LEN]
    row.lease_until = None
    if row.attempts >= MAX_ATTEMPTS:
        row.status = "DEAD"
    else:
        row.next_attempt_at = now + timedelta(seconds=BACKOFF_S[row.attempts - 1])
    status = row.status
    await session.commit()
    return status

"""Shared helpers for messaging DB tests (seed committed rows, always clean them up)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from app.messaging.consumers import CONSUMERS, Consumer
from app.messaging.rules import params_hash
from app.models.messaging import MsgDelivery, MsgEvent
from app.models.user import User
from tests._db import TestSession

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


@dataclass
class Seeded:
    event_id: uuid.UUID
    delivery_ids: list[uuid.UUID]


@asynccontextmanager
async def seeded(
    consumers: list[str],
    *,
    event_type: str = "it.pagopa.qa.test.thing.happened",
    severity: str = "info",
    data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> AsyncIterator[Seeded]:
    """Commit one event with one PENDING delivery per consumer, then delete it on exit."""
    event_id = uuid.uuid4()
    delivery_ids: list[uuid.UUID] = []
    async with TestSession() as s:
        s.add(
            MsgEvent(
                id=event_id,
                source="/t",
                type=event_type,
                severity=severity,
                data=data or {},
                time=NOW,
            )
        )
        await s.flush()
        for consumer in consumers:
            delivery_id = uuid.uuid4()
            delivery_ids.append(delivery_id)
            delivery_params = params or {}
            s.add(
                MsgDelivery(
                    id=delivery_id,
                    event_id=event_id,
                    consumer=consumer,
                    params=delivery_params,
                    params_hash=params_hash(delivery_params),
                    next_attempt_at=NOW,
                )
            )
        await s.commit()
    try:
        yield Seeded(event_id, delivery_ids)
    finally:
        async with TestSession() as s:
            await s.execute(delete(MsgEvent).where(MsgEvent.id == event_id))
            await s.commit()


async def get_delivery(delivery_id: uuid.UUID) -> MsgDelivery:
    async with TestSession() as s:
        return (
            await s.execute(
                select(MsgDelivery)
                .where(MsgDelivery.id == delivery_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()


def register(name: str, handler: Any) -> None:
    CONSUMERS[name] = Consumer(name=name, handler=handler)


@asynccontextmanager
async def users(*specs: tuple[str, bool]) -> AsyncIterator[list[uuid.UUID]]:
    """Commit one user per ``(role, is_active)``; delete them (and their inbox rows) on exit."""
    ids: list[uuid.UUID] = []
    async with TestSession() as s:
        for role, active in specs:
            user = User(
                email=f"{uuid.uuid4()}@example.org", name="Test", role=role, is_active=active
            )
            s.add(user)
            await s.flush()
            ids.append(user.id)
        await s.commit()
    try:
        yield ids
    finally:
        await delete_events_of(ids)
        async with TestSession() as s:
            await s.execute(delete(User).where(User.id.in_(ids)))
            await s.commit()


async def add_inbox(
    user_id: uuid.UUID,
    *,
    title: str = "t",
    severity: str = "info",
    type: str = "it.pagopa.qa.test.thing.happened",  # noqa: A002
    created_at: datetime = NOW,
    read: bool = False,
    link: str | None = None,
) -> uuid.UUID:
    """Commit an event and an inbox row for the user (the event is removed with its user)."""
    from app.models.messaging import MsgInbox

    event_id = uuid.uuid4()
    inbox_id = uuid.uuid4()
    async with TestSession() as s:
        s.add(
            MsgEvent(
                id=event_id, source="/t", type=type, severity=severity, data={}, time=created_at
            )
        )
        await s.flush()
        s.add(
            MsgInbox(
                id=inbox_id,
                event_id=event_id,
                user_id=user_id,
                title=title,
                body="b",
                link=link,
                severity=severity,
                type=type,
                created_at=created_at,
                read_at=created_at if read else None,
            )
        )
        await s.commit()
    return inbox_id


async def delete_events_of(user_ids: list[uuid.UUID]) -> None:
    """Remove the events behind the users' inbox rows (users deletion only cascades the rows)."""
    from app.models.messaging import MsgInbox

    async with TestSession() as s:
        event_ids = (
            (await s.execute(select(MsgInbox.event_id).where(MsgInbox.user_id.in_(user_ids))))
            .scalars()
            .all()
        )
        await s.execute(delete(MsgEvent).where(MsgEvent.id.in_(event_ids)))
        await s.commit()

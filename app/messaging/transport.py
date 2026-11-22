"""Transport seam of the messaging hub.

The transport moves accepted deliveries to handlers. Producers, routing rules and handlers do not
depend on it, so Azure Service Bus can replace the Postgres implementation later (design,
section 8): ``enqueue`` persists the accepted deliveries (the outbox) and ``drain_due`` hands due
deliveries to their handlers.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.messaging import dispatcher
from app.messaging.consumers import CONSUMERS, DeliveryContext
from app.messaging.envelope import CloudEvent
from app.messaging.rules import DeliverySpec, params_hash
from app.models.messaging import MsgDelivery, MsgEvent

logger = structlog.get_logger(__name__)


@dataclass
class DrainStats:
    claimed: int = 0
    done: int = 0
    failed: int = 0
    dead: int = 0


class Transport(Protocol):
    async def enqueue(
        self, session: AsyncSession, event_id: uuid.UUID, deliveries: Sequence[DeliverySpec]
    ) -> None: ...

    async def drain_due(self, *, limit: int = 50) -> DrainStats: ...


class PostgresTransport:
    """Reference transport: deliveries are rows of ``msg_deliveries`` drained by Celery."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is not None:
            return self._session_factory
        from app.core.db import async_session

        return async_session

    async def enqueue(
        self, session: AsyncSession, event_id: uuid.UUID, deliveries: Sequence[DeliverySpec]
    ) -> None:
        for spec in deliveries:
            await session.execute(
                pg_insert(MsgDelivery)
                .values(
                    id=uuid.uuid4(),
                    event_id=event_id,
                    consumer=spec.consumer,
                    params=spec.params,
                    params_hash=params_hash(spec.params),
                    next_attempt_at=self._clock(),
                )
                .on_conflict_do_nothing(constraint="uq_msg_deliveries_key")
            )

    async def drain_due(self, *, limit: int = 50) -> DrainStats:
        factory = self._factory()
        stats = DrainStats()
        async with factory() as session:
            claimed = await dispatcher.claim_due(session, limit=limit, now=self._clock())
        for item in claimed:
            stats.claimed += 1
            # Handlers run one after another, so the batch lease may have lapsed by now: renew
            # it for this item, or skip the item when another worker took it over.
            async with factory() as session:
                lease = await dispatcher.renew_lease(
                    session, item.delivery_id, item.lease_until, self._clock()
                )
            if lease is None:
                logger.warning("messaging_delivery_lost", delivery_id=str(item.delivery_id))
                continue
            error = await self._deliver(factory, item)
            async with factory() as session:
                if error is None:
                    if await dispatcher.record_success(
                        session, item.delivery_id, self._clock(), lease
                    ):
                        stats.done += 1
                    continue
                status = await dispatcher.record_failure(
                    session, item.delivery_id, error, self._clock(), lease
                )
            if status == "LOST":
                logger.warning("messaging_delivery_lost", delivery_id=str(item.delivery_id))
                continue
            stats.failed += 1
            if status == "DEAD":
                stats.dead += 1
                logger.error(
                    "messaging_delivery_dead",
                    delivery_id=str(item.delivery_id),
                    consumer=item.consumer,
                    error=error,
                )
            else:
                logger.warning(
                    "messaging_delivery_failed",
                    delivery_id=str(item.delivery_id),
                    consumer=item.consumer,
                    attempt=item.attempt,
                    error=error,
                )
        return stats

    async def _deliver(
        self, factory: async_sessionmaker[AsyncSession], item: dispatcher.Claimed
    ) -> str | None:
        """Run the handler outside any claim transaction; return an error text or None."""
        async with factory() as session:
            row = await session.get(MsgEvent, item.event_id)
            if row is None:
                return f"event {item.event_id} not found"
            event = CloudEvent(
                id=str(row.id),
                source=row.source,
                type=row.type,
                subject=row.subject,
                time=row.time,
                severity=row.severity,  # type: ignore[arg-type]
                product=row.product,
                environment=row.environment,  # type: ignore[arg-type]
                data=row.data,
            )
        consumer = CONSUMERS.get(item.consumer)
        if consumer is None:
            return f"unknown consumer {item.consumer}"
        async with factory() as handler_session:
            context = DeliveryContext(item.params, item.delivery_id, item.attempt, handler_session)
            try:
                await asyncio.wait_for(
                    consumer.handler(event, context), timeout=dispatcher.HANDLER_TIMEOUT_S
                )
                await handler_session.commit()
            except TimeoutError:
                await handler_session.rollback()
                return f"handler timeout after {dispatcher.HANDLER_TIMEOUT_S}s"
            except Exception as exc:
                await handler_session.rollback()
                return f"{type(exc).__name__}: {exc}"
        return None


_transport: Transport = PostgresTransport()


def get_transport() -> Transport:
    return _transport

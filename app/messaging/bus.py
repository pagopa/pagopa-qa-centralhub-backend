"""Producer-facing API: ``publish()``, ``clear_condition()`` and ``wake_dispatcher()``.

``publish`` and ``clear_condition`` never commit: events and deliveries are written in the
caller's transaction (transactional outbox), so the business change and the event commit or roll
back together. The caller commits and may then call ``wake_dispatcher()``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import structlog
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.messaging import rules as rules_module
from app.messaging.catalog import get_event_type
from app.messaging.envelope import CloudEvent
from app.messaging.rules import route
from app.messaging.transport import get_transport
from app.models.messaging import MsgDedupe, MsgEvent

logger = structlog.get_logger(__name__)


class PublishStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class PublishResult:
    status: PublishStatus
    event_id: str
    deliveries: int


async def _lock_dedupe_row(session: AsyncSession, key: str) -> MsgDedupe:
    """Create the key's row if missing and lock it, so concurrent publishes serialise."""
    await session.execute(
        pg_insert(MsgDedupe)
        .values(key=key, members=[])
        .on_conflict_do_nothing(index_elements=["key"])
    )
    result = await session.execute(
        select(MsgDedupe)
        .where(MsgDedupe.key == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def publish(session: AsyncSession, event: CloudEvent) -> PublishResult:
    """Validate, deduplicate, store and route an event, without committing."""
    event_type = get_event_type(event.type)
    event_type.data_model.model_validate(event.data)
    event_id = uuid.UUID(event.id)

    dedupe: MsgDedupe | None = None
    members: frozenset[str] = frozenset()
    if event_type.policy == "condition":
        assert event_type.key is not None and event_type.members is not None  # noqa: S101
        key = event_type.key(event)
        members = event_type.members(event)
        dedupe = await _lock_dedupe_row(session, key)
        if not members - set(dedupe.members):
            dedupe.members = sorted(members)
            logger.debug("messaging_event_suppressed", type=event.type, key=key)
            return PublishResult(PublishStatus.SUPPRESSED, event.id, 0)

    inserted = await session.execute(
        pg_insert(MsgEvent)
        .values(
            id=event_id,
            source=event.source,
            type=event.type,
            subject=event.subject,
            time=event.time,
            severity=event.severity,
            product=event.product,
            environment=event.environment,
            data=event.data,
        )
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(MsgEvent.id)
    )
    if inserted.scalar_one_or_none() is None:
        logger.info("messaging_event_duplicate", type=event.type, event_id=event.id)
        return PublishResult(PublishStatus.DUPLICATE, event.id, 0)

    specs = route(event, rules_module.RULES)
    await get_transport().enqueue(session, event_id, specs)

    if dedupe is not None:
        dedupe.members = sorted(members)
        dedupe.last_event_id = event_id
        dedupe.last_notified_at = datetime.now(UTC)

    logger.info(
        "messaging_event_accepted", type=event.type, event_id=event.id, deliveries=len(specs)
    )
    return PublishResult(PublishStatus.ACCEPTED, event.id, len(specs))


async def clear_condition(session: AsyncSession, key: str) -> None:
    """Forget a condition's reported members, so its next occurrence is announced again."""
    await session.execute(delete(MsgDedupe).where(MsgDedupe.key == key))


def _enqueue_dispatch() -> None:
    from app.tasks.messaging import dispatch_messages  # lazy: avoids an import cycle with Celery

    dispatch_messages.apply_async(expires=settings.messaging_dispatch_interval_s)


def wake_dispatcher() -> None:
    """Best effort: ask the dispatcher to run now. The beat schedule recovers from any failure."""
    try:
        _enqueue_dispatch()
    except Exception:
        logger.warning("messaging_wake_failed", exc_info=True)

"""Inbox consumer: one message per matching active user, rendered once and stored as a snapshot.

Idempotent: the unique ``(event_id, user_id)`` constraint makes a redelivery a no-op. A message
that belongs to a condition (``dedupe_key``) marks the same user's older unread messages with that
key as read (``auto_read``), so the unread counter reflects only the latest state.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.messaging.catalog import get_event_type
from app.messaging.consumers import Consumer, DeliveryContext
from app.messaging.envelope import CloudEvent
from app.models.messaging import MsgInbox
from app.models.user import User

logger = structlog.get_logger(__name__)


def merge_roles(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Two inbox deliveries for the same event become one addressed to the union of the roles."""
    return {"roles": sorted(set(a.get("roles", [])) | set(b.get("roles", [])))}


async def inbox_handler(event: CloudEvent, ctx: DeliveryContext) -> None:
    roles = list(ctx.params.get("roles", []))
    event_type = get_event_type(event.type)
    message = event_type.render(event)
    dedupe_key = (
        event_type.key(event) if event_type.policy == "condition" and event_type.key else None
    )

    user_ids = (
        (
            await ctx.session.execute(
                select(User.id).where(User.role.in_(roles), User.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    if not user_ids:
        logger.warning("messaging_inbox_no_recipients", event_id=event.id, roles=roles)
        return

    for user_id in user_ids:
        new_id = (
            await ctx.session.execute(
                pg_insert(MsgInbox)
                .values(
                    id=uuid.uuid4(),
                    event_id=uuid.UUID(event.id),
                    delivery_id=ctx.delivery_id,
                    user_id=user_id,
                    title=message.title,
                    body=message.body,
                    link=message.link,
                    severity=event.severity,
                    type=event.type,
                    dedupe_key=dedupe_key,
                )
                .on_conflict_do_nothing(constraint="uq_msg_inbox_event_user")
                .returning(MsgInbox.id)
            )
        ).scalar_one_or_none()
        if new_id is None or dedupe_key is None:
            continue
        await ctx.session.execute(
            update(MsgInbox)
            .where(
                MsgInbox.user_id == user_id,
                MsgInbox.dedupe_key == dedupe_key,
                MsgInbox.read_at.is_(None),
                MsgInbox.id != new_id,
            )
            .values(read_at=func.now(), auto_read=True)
        )


INBOX_CONSUMER = Consumer(name="inbox", handler=inbox_handler, merge=merge_roles)

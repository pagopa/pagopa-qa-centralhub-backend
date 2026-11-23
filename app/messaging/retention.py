"""Retention: events (and, by cascade, deliveries and inbox rows) and stale dedupe state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.messaging import MsgDedupe, MsgEvent


async def purge_older_than(session: AsyncSession, cutoff: datetime) -> dict[str, int]:
    """Delete events received, and dedupe rows updated, before ``cutoff``; commit."""
    events = await session.execute(delete(MsgEvent).where(MsgEvent.received_at < cutoff))
    dedupe = await session.execute(delete(MsgDedupe).where(MsgDedupe.updated_at < cutoff))
    await session.commit()
    assert isinstance(events, CursorResult) and isinstance(dedupe, CursorResult)  # noqa: S101
    return {"events": events.rowcount, "dedupe": dedupe.rowcount}

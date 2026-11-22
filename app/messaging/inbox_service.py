"""Read model of the inbox: the queries behind the bell and the Messages page."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, case, func, literal, select, tuple_, update
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.messaging import MsgInbox
from app.schemas.messaging import InboxItemOut, InboxPageOut, InboxSummaryOut

_RANK = case((MsgInbox.severity == "critical", 2), (MsgInbox.severity == "warning", 1), else_=0)
_SEVERITY_BY_RANK = {0: "info", 1: "warning", 2: "critical"}


def _cutoff() -> datetime:
    return datetime.now(UTC) - timedelta(days=settings.messaging_retention_days)


def _encode_cursor(created_at: datetime, inbox_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{inbox_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Parse an opaque cursor; raise ``ValueError`` when it is not one we issued."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        stamp, inbox_id = raw.split("|", 1)
        return datetime.fromisoformat(stamp), uuid.UUID(inbox_id)
    except Exception as exc:
        raise ValueError("invalid cursor") from exc


async def list_inbox(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    unread_only: bool = False,
    severity: str | None = None,
    type: str | None = None,  # noqa: A002 - query parameter name
    limit: int = 50,
    cursor: str | None = None,
) -> InboxPageOut:
    stmt = select(MsgInbox).where(MsgInbox.user_id == user_id, MsgInbox.created_at >= _cutoff())
    if unread_only:
        stmt = stmt.where(MsgInbox.read_at.is_(None))
    if severity:
        stmt = stmt.where(MsgInbox.severity == severity)
    if type:
        stmt = stmt.where(MsgInbox.type == type)
    if cursor:
        created_at, inbox_id = _decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(MsgInbox.created_at, MsgInbox.id)
            < tuple_(
                literal(created_at, DateTime(timezone=True)),
                literal(inbox_id, UUID(as_uuid=True)),
            )
        )
    rows = (
        (
            await db.execute(
                stmt.order_by(MsgInbox.created_at.desc(), MsgInbox.id.desc()).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    page = rows[:limit]
    next_cursor = (
        _encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit and page else None
    )
    return InboxPageOut(
        items=[InboxItemOut.model_validate(row) for row in page], next_cursor=next_cursor
    )


async def inbox_summary(db: AsyncSession, user_id: uuid.UUID) -> InboxSummaryOut:
    row = (
        await db.execute(
            select(
                func.count().label("total"),
                func.count().filter(MsgInbox.read_at.is_(None)).label("unread"),
                func.max(case((MsgInbox.read_at.is_(None), _RANK), else_=None)).label("rank"),
            ).where(MsgInbox.user_id == user_id, MsgInbox.created_at >= _cutoff())
        )
    ).one()
    severity = _SEVERITY_BY_RANK[row.rank] if row.rank is not None else None
    return InboxSummaryOut(
        unread=row.unread,
        total=row.total,
        max_unread_severity=severity,  # type: ignore[arg-type]
    )


async def mark_read(db: AsyncSession, user_id: uuid.UUID, inbox_id: uuid.UUID) -> bool:
    """Mark one of the user's messages read; False when it does not exist or is not theirs."""
    row = (
        await db.execute(
            select(MsgInbox).where(MsgInbox.id == inbox_id, MsgInbox.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    if row.read_at is None:
        row.read_at = datetime.now(UTC)
        await db.commit()
    return True


async def mark_all_read(db: AsyncSession, user_id: uuid.UUID) -> int:
    result = await db.execute(
        update(MsgInbox)
        .where(MsgInbox.user_id == user_id, MsgInbox.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    await db.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]

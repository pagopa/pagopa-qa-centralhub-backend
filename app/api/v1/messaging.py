from __future__ import annotations

import hmac
import json
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import ValidationError
from sqlalchemy import func, select

from app.config import settings
from app.deps import DbDep
from app.messaging import PublishStatus, inbox_service, publish, wake_dispatcher
from app.messaging.catalog import UnknownEventType
from app.messaging.envelope import CloudEvent
from app.models.audit_log import AuditLog
from app.models.user import User
from app.schemas.messaging import EventIngestOut, InboxPageOut, InboxSummaryOut, MarkReadOut

router = APIRouter()


async def get_inbox_user(
    db: DbDep, x_user_email: Annotated[str | None, Header()] = None
) -> User | None:
    """Current user for the inbox routes.

    Interim identity: the frontend sends the session email in ``X-User-Email`` because the backend
    does not verify JWTs yet (``verify_jwt``). When it does, only this dependency changes. A missing
    header is a client error (401); an unknown or inactive user simply has an empty inbox.
    """
    if not x_user_email or not x_user_email.strip():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-User-Email header")
    user = (
        await db.execute(select(User).where(func.lower(User.email) == x_user_email.strip().lower()))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


InboxUser = Annotated[User | None, Depends(get_inbox_user)]
SeverityQuery = Annotated[Literal["info", "warning", "critical"] | None, Query()]


@router.get("/inbox", response_model=InboxPageOut)
async def list_inbox(
    db: DbDep,
    user: InboxUser,
    unread_only: bool = False,
    severity: SeverityQuery = None,
    type: Annotated[str | None, Query(max_length=255)] = None,  # noqa: A002
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
) -> InboxPageOut:
    if user is None:
        return InboxPageOut(items=[], next_cursor=None)
    try:
        return await inbox_service.list_inbox(
            db,
            user.id,
            unread_only=unread_only,
            severity=severity,
            type=type,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/inbox/summary", response_model=InboxSummaryOut)
async def inbox_summary(db: DbDep, user: InboxUser) -> InboxSummaryOut:
    if user is None:
        return InboxSummaryOut(unread=0, total=0, max_unread_severity=None)
    return await inbox_service.inbox_summary(db, user.id)


@router.post("/inbox/read-all", response_model=MarkReadOut)
async def read_all(db: DbDep, user: InboxUser) -> MarkReadOut:
    if user is None:
        return MarkReadOut(updated=0)
    return MarkReadOut(updated=await inbox_service.mark_all_read(db, user.id))


@router.post("/inbox/{inbox_id}/read", response_model=MarkReadOut)
async def read_one(inbox_id: uuid.UUID, db: DbDep, user: InboxUser) -> MarkReadOut:
    if user is None or not await inbox_service.mark_read(db, user.id, inbox_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    return MarkReadOut(updated=1)


def require_ingest_key(
    x_hub_ingest_key: Annotated[str | None, Header()] = None,
) -> None:
    """Interim guard for external producers: a shared secret in ``X-Hub-Ingest-Key``."""
    expected = settings.messaging_ingest_key
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Event ingest is not configured")
    if not x_hub_ingest_key or not hmac.compare_digest(
        x_hub_ingest_key.encode(), expected.encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid ingest key")


@router.post(
    "/events",
    response_model=EventIngestOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_ingest_key)],
)
async def ingest_event(event: CloudEvent, db: DbDep, response: Response) -> EventIngestOut:
    """Accept one CloudEvent (202), or report it as duplicate or suppressed (200)."""
    try:
        result = await publish(db, event)
    except UnknownEventType as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown event type: {exc}"
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, json.loads(exc.json(include_url=False))
        ) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    db.add(
        AuditLog(
            actor_id=None,
            action="messaging.event.ingest",
            resource_type="msg_event",
            resource_id=event.id,
            detail=json.dumps({"type": event.type, "status": result.status.value}),
        )
    )
    await db.commit()
    if result.status is PublishStatus.ACCEPTED:
        wake_dispatcher()
    else:
        response.status_code = status.HTTP_200_OK
    return EventIngestOut(id=result.event_id, status=result.status.value)

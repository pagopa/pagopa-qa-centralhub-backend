from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.status_page import SpApplication, SpVersionState
from app.services.status_page_sources import Reading
from app.services.status_page_sync import apply_reading

logger = structlog.get_logger(__name__)

MANUAL_TARGETS = ("DEV", "UAT", "PROD")
MAX_VERSION_LENGTH = 100


async def _states_of(db: AsyncSession, app_id: uuid.UUID) -> dict[str, SpVersionState]:
    rows = await db.execute(
        select(SpVersionState).where(
            SpVersionState.application_id == app_id, SpVersionState.target.in_(MANUAL_TARGETS)
        )
    )
    return {state.target: state for state in rows.scalars()}


async def make_manual(db: AsyncSession, app: SpApplication, now: datetime) -> None:
    """Switch the DEV/UAT/PROD states of ``app`` to MANUAL, keeping the last good version.

    Missing rows are created empty; existing rows keep version and dates and only lose the
    read error. No deploy event is written. The caller commits.
    """
    try:
        states = await _states_of(db, app.id)
        for target in MANUAL_TARGETS:
            state = states.get(target)
            if state is None:
                db.add(
                    SpVersionState(
                        application_id=app.id,
                        target=target,
                        version=None,
                        read_status="MANUAL",
                        error_detail=None,
                        checked_at=now,
                    )
                )
                continue
            state.read_status = "MANUAL"
            state.error_detail = None
        await db.flush()
    except Exception:
        logger.exception("make_manual failed")
        raise


def _normalize(versions: dict[str, str | None]) -> dict[str, str | None]:
    normalized: dict[str, str | None] = {}
    for target, raw in versions.items():
        if target not in MANUAL_TARGETS:
            raise ValueError(f"Invalid manual version target: {target}")
        value = (raw or "").strip()
        if len(value) > MAX_VERSION_LENGTH:
            raise ValueError(f"Version for {target} exceeds {MAX_VERSION_LENGTH} characters")
        normalized[target] = value or None
    return normalized


async def set_manual_versions(
    db: AsyncSession,
    app: SpApplication,
    versions: dict[str, str | None],
    user: str,
    now: datetime,
) -> dict[str, tuple[str | None, str | None]]:
    """Set (or clear) the manually entered versions of ``app``; return the changed targets.

    A new value goes through ``apply_reading`` and may add a deploy event; the same value only
    refreshes the check dates; an empty value clears the version but keeps the row. The caller
    commits.
    """
    try:
        wanted = _normalize(versions)
        states = await _states_of(db, app.id)
        changed: dict[str, tuple[str | None, str | None]] = {}
        for target, value in wanted.items():
            state = states.get(target)
            before = state.version if state is not None else None
            if value is None:
                if state is None:
                    db.add(
                        SpVersionState(
                            application_id=app.id,
                            target=target,
                            version=None,
                            read_status="MANUAL",
                            checked_at=now,
                        )
                    )
                    continue
                state.read_status = "MANUAL"
                state.error_detail = None
                state.checked_at = now
                if before is not None:
                    state.version = None
                    state.version_since = None
                    changed[target] = (before, None)
                continue
            if state is not None and before == value:
                state.read_status = "MANUAL"
                state.error_detail = None
                state.checked_at = now
                state.last_ok_at = now
                continue
            new_state, event = apply_reading(
                state, Reading(target, "MANUAL", value, None), now, app.id
            )
            if state is None:
                db.add(new_state)
            if event is not None:
                db.add(event)
            changed[target] = (before, value)
        if changed:
            db.add(
                AuditLog(
                    actor_id=None,
                    action="status_page.application.versions",
                    resource_type="sp_application",
                    resource_id=str(app.id),
                    detail=json.dumps(
                        {
                            "user": user,
                            "before": {t: b for t, (b, _) in changed.items()},
                            "after": {t: a for t, (_, a) in changed.items()},
                        }
                    ),
                )
            )
        await db.flush()
        return changed
    except Exception:
        logger.exception("set_manual_versions failed")
        raise


async def manual_versions_of(
    db: AsyncSession, app_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, dict[str, str | None]]:
    """Current DEV/UAT/PROD versions per application, in one query."""
    try:
        result: dict[uuid.UUID, dict[str, str | None]] = {
            app_id: dict.fromkeys(MANUAL_TARGETS) for app_id in app_ids
        }
        if not app_ids:
            return result
        rows = await db.execute(
            select(
                SpVersionState.application_id, SpVersionState.target, SpVersionState.version
            ).where(
                SpVersionState.application_id.in_(app_ids),
                SpVersionState.target.in_(MANUAL_TARGETS),
            )
        )
        for app_id, target, version in rows.all():
            result[app_id][target] = version
        return result
    except Exception:
        logger.exception("manual_versions_of failed")
        raise

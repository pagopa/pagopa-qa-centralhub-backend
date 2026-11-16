from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.db import engine as app_engine
from app.models.status_page import SpApplication, SpDeployEvent, SpSyncStatus, SpVersionState
from app.services.status_page_sources import Reading, read_env, read_repo

logger = structlog.get_logger(__name__)

SYNC_LOCK_ID = 671
MAX_ERRORS = 20
QUEUED_STALE_AFTER = timedelta(minutes=10)
RUNNING_STALE_AFTER = timedelta(minutes=30)
ENV_TARGETS = ("DEV", "UAT", "PROD")


class StatusPageSyncInProgressError(Exception):
    """Raised when a status page synchronization is already queued or running."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def apply_reading(
    state: SpVersionState | None,
    reading: Reading,
    now: datetime,
    application_id: uuid.UUID,
) -> tuple[SpVersionState, SpDeployEvent | None]:
    """Apply one reading to the current state. Pure: no DB access.

    The given state is updated in place (or a new one is created) and an event is
    returned only when an OK read differs from the last good version.
    """
    if state is None:
        state = SpVersionState(application_id=application_id, target=reading.target)
    state.read_status = reading.read_status
    state.error_detail = reading.error_detail
    state.checked_at = now
    if reading.read_status != "OK" or reading.version is None:
        return state, None

    event: SpDeployEvent | None = None
    if state.version is None:
        event = SpDeployEvent(
            application_id=application_id,
            target=reading.target,
            previous_version=None,
            version=reading.version,
            detected_at=now,
            previous_seen_at=None,
            is_baseline=True,
        )
        state.version_since = now
    elif state.version != reading.version:
        event = SpDeployEvent(
            application_id=application_id,
            target=reading.target,
            previous_version=state.version,
            version=reading.version,
            detected_at=now,
            previous_seen_at=state.last_ok_at,
            is_baseline=False,
        )
        state.version_since = now
    state.version = reading.version
    state.last_ok_at = now
    return state, event


async def get_sync_status(db: AsyncSession) -> SpSyncStatus | None:
    try:
        status = await db.get(SpSyncStatus, 1)
        if status is None:
            logger.info("get_sync_status query succeeded with 0 results")
        return status
    except Exception:
        logger.exception("get_sync_status failed")
        raise


def _is_stale(status: SpSyncStatus, now: datetime) -> bool:
    if status.status == "QUEUED":
        return status.queued_at is None or now - status.queued_at > QUEUED_STALE_AFTER
    if status.status == "RUNNING":
        return status.started_at is None or now - status.started_at > RUNNING_STALE_AFTER
    return True


async def request_manual_sync(
    db: AsyncSession, *, user: str, now: datetime | None = None
) -> SpSyncStatus:
    try:
        now = now or _utcnow()
        status = (
            await db.execute(select(SpSyncStatus).where(SpSyncStatus.id == 1).with_for_update())
        ).scalar_one_or_none()
        if status is None:
            status = SpSyncStatus(id=1, status="QUEUED", trigger="MANUAL")
            db.add(status)
        elif status.status in ("QUEUED", "RUNNING") and not _is_stale(status, now):
            await db.rollback()
            raise StatusPageSyncInProgressError("Status page synchronization already in progress")
        status.status = "QUEUED"
        status.trigger = "MANUAL"
        status.requested_by = user
        status.queued_at = now
        status.started_at = None
        status.finished_at = None
        status.celery_task_id = None
        status.applications_checked = 0
        status.reads_failed = 0
        status.errors = []
        await db.commit()
        return status
    except StatusPageSyncInProgressError:
        raise
    except Exception:
        logger.exception("request_manual_sync failed")
        raise


async def set_sync_task_id(db: AsyncSession, task_id: str) -> None:
    try:
        status = await db.get(SpSyncStatus, 1)
        if status is None:
            logger.info("set_sync_task_id found no sync status row")
            return
        status.celery_task_id = task_id
        await db.commit()
    except Exception:
        logger.exception("set_sync_task_id failed")
        raise


async def _get_or_create_status(db: AsyncSession) -> SpSyncStatus:
    status = await db.get(SpSyncStatus, 1)
    if status is None:
        status = SpSyncStatus(id=1, status="RUNNING", trigger="SCHEDULED")
        db.add(status)
    return status


async def _record_failed_sync_status(db: AsyncSession, *, error: str) -> None:
    try:
        await db.rollback()
        try:
            async with db.begin():
                status = await _get_or_create_status(db)
                status.status = "FAILED"
                status.finished_at = _utcnow()
                status.errors = [error[:500]]
        except Exception:
            logger.exception("_record_failed_sync_status failed")
            await db.rollback()
    except Exception:
        logger.exception("_record_failed_sync_status failed")
        raise


async def _load_applications(db: AsyncSession) -> list[SpApplication]:
    result = await db.scalars(select(SpApplication).order_by(SpApplication.name))
    return list(result)


async def _read_all(
    applications: list[SpApplication],
) -> list[tuple[SpApplication, Reading]]:
    semaphore = asyncio.Semaphore(settings.status_page_sync_concurrency)

    async def _guarded(
        app: SpApplication, client: httpx.AsyncClient, target: str
    ) -> tuple[SpApplication, Reading]:
        async with semaphore:
            try:
                if target == "REPO":
                    return app, await read_repo(client, app.repository)
                return app, await read_env(client, target, app.product_key)
            except Exception as exc:
                logger.exception("read failed", application=app.name, target=target)
                return app, Reading(target, "ERROR", None, f"{type(exc).__name__}: {exc}"[:500])

    async with httpx.AsyncClient(timeout=settings.status_page_http_timeout_s) as client:
        tasks = [
            _guarded(app, client, target)
            for app in applications
            for target in ("REPO", *ENV_TARGETS)
        ]
        return list(await asyncio.gather(*tasks))


async def _run_locked(db: AsyncSession, *, trigger: str, requested_by: str | None) -> SpSyncStatus:
    now = _utcnow()
    status = await _get_or_create_status(db)
    status.status = "RUNNING"
    status.trigger = trigger
    status.requested_by = requested_by
    status.started_at = now
    status.finished_at = None
    await db.commit()

    if not settings.github_token:
        message = "GitHub token is not configured"
        logger.error("run_sync failed", error=message)
        await _record_failed_sync_status(db, error=message)
        raise RuntimeError(message)

    try:
        applications = await _load_applications(db)
        if not applications:
            logger.info("_run_sync query succeeded with 0 results")
        readings = await _read_all(applications)
        checked_at = _utcnow()

        ids = [app.id for app in applications]
        states: dict[tuple[uuid.UUID, str], SpVersionState] = {}
        if ids:
            existing = await db.scalars(
                select(SpVersionState).where(SpVersionState.application_id.in_(ids))
            )
            states = {(s.application_id, s.target): s for s in existing}

        errors: list[str] = []
        reads_failed = 0
        for app, reading in readings:
            state, event = apply_reading(
                states.get((app.id, reading.target)), reading, checked_at, app.id
            )
            if (app.id, reading.target) not in states:
                db.add(state)
                states[(app.id, reading.target)] = state
            if event is not None:
                db.add(event)
            if reading.read_status != "OK":
                reads_failed += 1
                if len(errors) < MAX_ERRORS:
                    detail = reading.error_detail or reading.read_status
                    errors.append(f"{app.name} {reading.target}: {detail}")

        finished = _utcnow()
        status = await _get_or_create_status(db)
        status.status = "PARTIAL" if reads_failed else "OK"
        status.finished_at = finished
        status.last_success_at = finished
        status.applications_checked = len(applications)
        status.reads_failed = reads_failed
        status.errors = errors
        await db.commit()
        return status
    except Exception as exc:
        logger.exception("run_sync failed")
        await _record_failed_sync_status(db, error=f"{type(exc).__name__}: {exc}")
        raise


async def run_sync(db: AsyncSession, *, trigger: str, requested_by: str | None) -> SpSyncStatus:
    try:
        bind = db.bind
        lock_engine = bind if bind is not None and hasattr(bind, "connect") else app_engine
        async with lock_engine.connect() as lock_conn:
            acquired = bool(await lock_conn.scalar(select(func.pg_try_advisory_lock(SYNC_LOCK_ID))))
            if not acquired:
                logger.info("run_sync skipped: another synchronization holds the lock")
                existing = await get_sync_status(db)
                if existing is not None:
                    db.expunge(existing)
                await db.rollback()
                return existing or SpSyncStatus(id=1, status="RUNNING", trigger=trigger)
            try:
                return await _run_locked(db, trigger=trigger, requested_by=requested_by)
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"), {"id": SYNC_LOCK_ID}
                )
                await lock_conn.commit()
    except Exception:
        logger.exception("run_sync failed")
        raise

from __future__ import annotations

# ruff: noqa: S101
import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text

from app.config import settings
from app.models.status_page import (
    SpApplication,
    SpDeployEvent,
    SpProduct,
    SpSyncStatus,
    SpVersionState,
)
from app.services import status_page_sync as svc
from app.services.status_page_sources import Reading
from app.services.status_page_sync import (
    SYNC_LOCK_ID,
    StatusPageSyncInProgressError,
    apply_reading,
    request_manual_sync,
    run_sync,
)
from tests._db import TestSession

APP = uuid.uuid4()
T0 = datetime(2026, 1, 1, 10, tzinfo=UTC)
T1 = T0 + timedelta(minutes=15)
T2 = T0 + timedelta(minutes=30)


def ok(v: str, target: str = "DEV") -> Reading:
    return Reading(target, "OK", v, None)


def err(target: str = "DEV") -> Reading:
    return Reading(target, "ERROR", None, "HTTP 503")


def st(version: str, last_ok: datetime) -> SpVersionState:
    return SpVersionState(
        application_id=APP,
        target="DEV",
        version=version,
        read_status="OK",
        checked_at=last_ok,
        last_ok_at=last_ok,
        version_since=last_ok,
    )


def test_first_ok_is_baseline() -> None:
    state, ev = apply_reading(None, ok("1.4.1"), T0, APP)
    assert ev is not None and ev.is_baseline and ev.previous_version is None
    assert state.version_since == T0 and state.version == "1.4.1" and state.last_ok_at == T0


def test_same_version_no_event() -> None:
    state, ev = apply_reading(st("1.4.1", T0), ok("1.4.1"), T1, APP)
    assert ev is None and state.last_ok_at == T1 and state.version_since == T0


def test_error_keeps_last_version_no_event() -> None:
    state, ev = apply_reading(st("1.4.1", T0), err(), T1, APP)
    assert ev is None and state.version == "1.4.1" and state.read_status == "ERROR"
    assert state.last_ok_at == T0 and state.checked_at == T1


def test_error_then_same_version_no_event() -> None:
    s1, _ = apply_reading(st("1.4.1", T0), err(), T1, APP)
    s2, ev = apply_reading(s1, ok("1.4.1"), T2, APP)
    assert ev is None and s2.read_status == "OK" and s2.error_detail is None


def test_error_then_new_version_one_event() -> None:
    s1, _ = apply_reading(st("1.4.1", T0), err(), T1, APP)
    _, ev = apply_reading(s1, ok("1.4.2"), T2, APP)
    assert ev is not None
    assert (
        ev.previous_version,
        ev.version,
        ev.previous_seen_at,
        ev.detected_at,
        ev.is_baseline,
    ) == (
        "1.4.1",
        "1.4.2",
        T0,
        T2,
        False,
    )


def test_first_read_error_creates_state_without_version() -> None:
    state, ev = apply_reading(None, err(), T0, APP)
    assert ev is None and state.version is None and state.read_status == "ERROR"


def test_downgrade_is_event() -> None:
    _, ev = apply_reading(st("1.4.2", T0), ok("1.4.1"), T1, APP)
    assert ev is not None and ev.version == "1.4.1" and not ev.is_baseline


def test_no_release_is_not_ok() -> None:
    state, ev = apply_reading(None, Reading("REPO", "NO_RELEASE", None, None), T0, APP)
    assert ev is None and state.read_status == "NO_RELEASE"


def test_v_prefix_change_is_event_exact_strings() -> None:
    _, ev = apply_reading(st("1.4.1", T0), ok("v1.4.1"), T1, APP)
    assert ev is not None


# ---------------------------------------------------------------- DB tests


@pytest.fixture(autouse=True)
def _github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_token", "test-token")


@pytest_asyncio.fixture
async def catalog() -> AsyncIterator[list[SpApplication]]:
    suffix = uuid.uuid4().hex[:8]
    async with TestSession() as db:
        original = await db.get(SpSyncStatus, 1)
        snapshot: dict[str, Any] | None = None
        if original is not None:
            snapshot = {c.name: getattr(original, c.name) for c in SpSyncStatus.__table__.columns}
        product = SpProduct(name=f"zz-test-{suffix}", area="CORE")
        db.add(product)
        await db.flush()
        apps = [
            SpApplication(
                product_id=product.id,
                name=f"zz-app-{i}-{suffix}",
                product_key=f"zz-key-{i}-{suffix}",
                repository=f"zz-repo-{i}",
                runtime="AKS",
                ci="GHA",
                kind="backend",
            )
            for i in range(2)
        ]
        db.add_all(apps)
        await db.commit()
    try:
        yield apps
    finally:
        async with TestSession() as db:
            ids = [a.id for a in apps]
            await db.execute(delete(SpDeployEvent).where(SpDeployEvent.application_id.in_(ids)))
            await db.execute(delete(SpVersionState).where(SpVersionState.application_id.in_(ids)))
            await db.execute(delete(SpApplication).where(SpApplication.id.in_(ids)))
            await db.execute(delete(SpProduct).where(SpProduct.id == apps[0].product_id))
            await db.execute(delete(SpSyncStatus))
            if snapshot is not None:
                db.add(SpSyncStatus(**snapshot))
            await db.commit()


def _patch_sources(apps: list[SpApplication], *, fail_for: str | None = None) -> Any:
    async def fake_repo(client: Any, repository: str) -> Reading:
        return ok("1.0.0", "REPO")

    async def fake_env(client: Any, target: str, product_key: str) -> Reading:
        if fail_for and product_key == fail_for and target == "UAT":
            return err("UAT")
        return ok("1.0.0", target)

    async def fake_load(db: Any) -> list[SpApplication]:
        return apps

    return (
        patch.object(svc, "read_repo", AsyncMock(side_effect=fake_repo)),
        patch.object(svc, "read_env", AsyncMock(side_effect=fake_env)),
        patch.object(svc, "_load_applications", fake_load),
    )


async def _counts(ids: list[uuid.UUID]) -> tuple[int, int]:
    async with TestSession() as db:
        states = await db.scalar(
            select(func.count())
            .select_from(SpVersionState)
            .where(SpVersionState.application_id.in_(ids))
        )
        events = await db.scalar(
            select(func.count())
            .select_from(SpDeployEvent)
            .where(SpDeployEvent.application_id.in_(ids), SpDeployEvent.is_baseline.is_(True))
        )
        return states or 0, events or 0


@pytest.mark.asyncio
async def test_run_sync_all_ok(catalog: list[SpApplication]) -> None:
    p1, p2, p3 = _patch_sources(catalog)
    with p1, p2, p3:
        async with TestSession() as db:
            status = await run_sync(db, trigger="SCHEDULED", requested_by=None)
    assert status.status == "OK"
    assert status.applications_checked == 2 and status.reads_failed == 0
    assert status.last_success_at is not None and status.errors == []
    assert await _counts([a.id for a in catalog]) == (8, 8)


@pytest.mark.asyncio
async def test_run_sync_second_run_creates_no_events(catalog: list[SpApplication]) -> None:
    p1, p2, p3 = _patch_sources(catalog)
    with p1, p2, p3:
        async with TestSession() as db:
            await run_sync(db, trigger="SCHEDULED", requested_by=None)
        async with TestSession() as db:
            await run_sync(db, trigger="MANUAL", requested_by="a@b.c")
    async with TestSession() as db:
        total = await db.scalar(
            select(func.count())
            .select_from(SpDeployEvent)
            .where(SpDeployEvent.application_id.in_([a.id for a in catalog]))
        )
        status = await db.get(SpSyncStatus, 1)
    assert total == 8
    assert status is not None and status.trigger == "MANUAL" and status.requested_by == "a@b.c"


@pytest.mark.asyncio
async def test_run_sync_skips_application_deleted_during_reads(
    catalog: list[SpApplication],
) -> None:
    kept, deleted = catalog
    p1, p2, p3 = _patch_sources(catalog)
    deleted_once = False

    async def delete_while_reading(client: Any, repository: str) -> Reading:
        nonlocal deleted_once
        if repository == deleted.repository and not deleted_once:
            deleted_once = True
            async with TestSession() as other:
                await other.execute(delete(SpApplication).where(SpApplication.id == deleted.id))
                await other.commit()
        return ok("1.0.0", "REPO")

    with p1, p2, p3, patch.object(svc, "read_repo", AsyncMock(side_effect=delete_while_reading)):
        async with TestSession() as db:
            status = await run_sync(db, trigger="SCHEDULED", requested_by=None)

    assert deleted_once
    assert status.status == "OK"
    assert status.applications_checked == 1 and status.reads_failed == 0
    assert await _counts([kept.id]) == (4, 4)
    assert await _counts([deleted.id]) == (0, 0)


@pytest.mark.asyncio
async def test_run_sync_partial(catalog: list[SpApplication]) -> None:
    p1, p2, p3 = _patch_sources(catalog, fail_for=catalog[0].product_key)
    with p1, p2, p3:
        async with TestSession() as db:
            status = await run_sync(db, trigger="SCHEDULED", requested_by=None)
    assert status.status == "PARTIAL" and status.reads_failed == 1
    assert len(status.errors) == 1
    assert catalog[0].name in status.errors[0] and "UAT" in status.errors[0]


@pytest.mark.asyncio
async def test_run_sync_db_failure_records_failed(catalog: list[SpApplication]) -> None:
    p1, p2, p3 = _patch_sources(catalog)
    with p1, p2, p3:
        async with TestSession() as db:
            real_commit = db.commit
            calls = {"n": 0}

            async def flaky_commit() -> None:
                calls["n"] += 1
                if calls["n"] == 2:  # the persist commit (first one marks RUNNING)
                    raise RuntimeError("db boom")
                await real_commit()

            with patch.object(db, "commit", flaky_commit), pytest.raises(RuntimeError):
                await run_sync(db, trigger="SCHEDULED", requested_by=None)
    async with TestSession() as db:
        status = await db.get(SpSyncStatus, 1)
    assert status is not None and status.status == "FAILED"
    assert status.finished_at is not None and "db boom" in status.errors[0]
    assert await _counts([a.id for a in catalog]) == (0, 0)


@pytest.mark.asyncio
async def test_run_sync_returns_when_lock_held(catalog: list[SpApplication]) -> None:
    async with TestSession() as db:
        db.add(SpSyncStatus(id=1, status="OK", trigger="SCHEDULED", applications_checked=7))
        await db.commit()
    from tests._db import test_engine

    p1, p2, p3 = _patch_sources(catalog)
    async with test_engine.connect() as other:
        await other.scalar(select(func.pg_advisory_lock(SYNC_LOCK_ID)))
        try:
            with p1 as repo, p2, p3:
                async with TestSession() as db:
                    status = await run_sync(db, trigger="SCHEDULED", requested_by=None)
                repo.assert_not_called()
        finally:
            await other.execute(text("SELECT pg_advisory_unlock(:i)"), {"i": SYNC_LOCK_ID})
            await other.commit()
    assert status.status == "OK" and status.applications_checked == 7
    async with TestSession() as db:
        row = await db.get(SpSyncStatus, 1)
    assert row is not None and row.status == "OK" and row.applications_checked == 7


async def _set_status(**fields: Any) -> None:
    async with TestSession() as db:
        await db.execute(delete(SpSyncStatus))
        db.add(SpSyncStatus(id=1, trigger="SCHEDULED", **fields))
        await db.commit()


@pytest.mark.asyncio
async def test_manual_sync_blocked_while_running(catalog: list[SpApplication]) -> None:
    now = datetime.now(UTC)
    await _set_status(status="RUNNING", started_at=now - timedelta(minutes=5))
    async with TestSession() as db:
        with pytest.raises(StatusPageSyncInProgressError):
            await request_manual_sync(db, user="a@b.c", now=now)


@pytest.mark.asyncio
async def test_manual_sync_stale_running_allowed(catalog: list[SpApplication]) -> None:
    now = datetime.now(UTC)
    await _set_status(status="RUNNING", started_at=now - timedelta(minutes=31))
    async with TestSession() as db:
        status = await request_manual_sync(db, user="a@b.c", now=now)
    assert (status.status, status.trigger, status.requested_by) == ("QUEUED", "MANUAL", "a@b.c")


@pytest.mark.asyncio
async def test_manual_sync_stale_queued_allowed_fresh_queued_blocks(
    catalog: list[SpApplication],
) -> None:
    now = datetime.now(UTC)
    await _set_status(status="QUEUED", queued_at=now - timedelta(minutes=11))
    async with TestSession() as db:
        assert (await request_manual_sync(db, user="a@b.c", now=now)).status == "QUEUED"
    await _set_status(status="QUEUED", queued_at=now - timedelta(minutes=2))
    async with TestSession() as db:
        with pytest.raises(StatusPageSyncInProgressError):
            await request_manual_sync(db, user="a@b.c", now=now)


@pytest.mark.asyncio
async def test_manual_sync_without_row_creates_it(catalog: list[SpApplication]) -> None:
    async with TestSession() as db:
        await db.execute(delete(SpSyncStatus))
        await db.commit()
        status = await request_manual_sync(db, user="a@b.c")
    assert status.status == "QUEUED"


@pytest.mark.asyncio
async def test_concurrent_manual_requests_without_row_give_one_conflict(
    catalog: list[SpApplication],
) -> None:
    async with TestSession() as db:
        await db.execute(delete(SpSyncStatus))
        await db.commit()

    async def request(user: str) -> SpSyncStatus:
        async with TestSession() as db:
            return await request_manual_sync(db, user=user)

    results = await asyncio.gather(
        request("a@b.c"), request("d@e.f"), return_exceptions=True
    )

    queued = [r for r in results if isinstance(r, SpSyncStatus)]
    conflicts = [r for r in results if isinstance(r, StatusPageSyncInProgressError)]
    assert len(queued) == 1 and len(conflicts) == 1, results
    assert queued[0].status == "QUEUED"


@pytest.mark.asyncio
async def test_concurrent_status_creation_without_row_does_not_collide(
    catalog: list[SpApplication],
) -> None:
    async with TestSession() as db:
        await db.execute(delete(SpSyncStatus))
        await db.commit()

    async def mark(status_value: str) -> None:
        async with TestSession() as db:
            status = await svc._get_or_create_status(db)
            status.status = status_value
            await db.commit()

    await asyncio.gather(mark("RUNNING"), mark("FAILED"))

    async with TestSession() as db:
        rows = list(await db.scalars(select(SpSyncStatus)))
    assert len(rows) == 1 and rows[0].status in ("RUNNING", "FAILED")


@pytest.mark.asyncio
async def test_run_sync_fails_without_github_token(
    catalog: list[SpApplication], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "github_token", "")
    p1, p2, p3 = _patch_sources(catalog)
    with p1 as repo, p2 as env, p3:
        async with TestSession() as db:
            with pytest.raises(RuntimeError, match="GitHub token is not configured"):
                await run_sync(db, trigger="SCHEDULED", requested_by=None)
        repo.assert_not_called()
        env.assert_not_called()
    async with TestSession() as db:
        status = await db.get(SpSyncStatus, 1)
    assert status is not None and status.status == "FAILED"
    assert status.errors == ["GitHub token is not configured"]
    assert await _counts([a.id for a in catalog]) == (0, 0)


@pytest.mark.asyncio
async def test_set_sync_task_id_stores_id(catalog: list[SpApplication]) -> None:
    from app.services.status_page_sync import set_sync_task_id

    async with TestSession() as db:
        await request_manual_sync(db, user="a@b.c")
        await set_sync_task_id(db, "task-123")
    async with TestSession() as db:
        row = await db.get(SpSyncStatus, 1)
        assert row is not None
        assert row.celery_task_id == "task-123"


@pytest.mark.asyncio
async def test_mark_enqueue_failed_releases_the_queue(catalog: list[SpApplication]) -> None:
    from app.services.status_page_sync import mark_enqueue_failed

    async with TestSession() as db:
        await request_manual_sync(db, user="a@b.c")
        await mark_enqueue_failed(db, "Impossibile accodare la sincronizzazione")
    async with TestSession() as db:
        row = await db.get(SpSyncStatus, 1)
        assert row is not None
        assert row.status == "FAILED"
        assert row.errors == ["Impossibile accodare la sincronizzazione"]
        # a new manual sync is no longer blocked
        assert (await request_manual_sync(db, user="a@b.c")).status == "QUEUED"


def _sync_row(status: str, *, queued_ago: int | None = None, started_ago: int | None = None) -> Any:
    now = datetime.now(UTC)
    return SpSyncStatus(
        id=1,
        status=status,
        trigger="SCHEDULED",
        requested_by=None,
        queued_at=now - timedelta(minutes=queued_ago) if queued_ago is not None else None,
        started_at=now - timedelta(minutes=started_ago) if started_ago is not None else None,
        finished_at=None,
        last_success_at=None,
        applications_checked=0,
        reads_failed=0,
        errors=[],
    )


def test_sync_status_out_in_progress_follows_stale_rule() -> None:
    from app.schemas.status_page import SyncStatusOut

    def in_progress(row: Any) -> bool:
        return SyncStatusOut.model_validate(row).model_dump()["in_progress"]

    assert in_progress(_sync_row("RUNNING", started_ago=1)) is True
    assert in_progress(_sync_row("RUNNING", started_ago=31)) is False
    assert in_progress(_sync_row("QUEUED", queued_ago=1)) is True
    assert in_progress(_sync_row("QUEUED", queued_ago=11)) is False
    assert in_progress(_sync_row("OK")) is False

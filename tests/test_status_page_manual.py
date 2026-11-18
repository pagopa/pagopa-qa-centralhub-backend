from __future__ import annotations

# ruff: noqa: S101
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models.audit_log import AuditLog
from app.models.status_page import SpApplication, SpDeployEvent, SpProduct, SpVersionState
from app.services.status_page import VersionNotFoundError, get_version
from app.services.status_page_manual import (
    MANUAL_TARGETS,
    make_manual,
    manual_versions_of,
    set_manual_versions,
)
from app.services.status_page_sources import Reading
from app.services.status_page_sync import apply_reading
from tests._db import TestSession

T0 = datetime(2026, 4, 1, 10, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
USER = "tester@example.com"


class Ctx:
    def __init__(self) -> None:
        self.product_id = uuid.uuid4()
        self.app_id = uuid.uuid4()
        self.key = f"zz-manual-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def ctx() -> AsyncIterator[Ctx]:
    c = Ctx()
    async with TestSession() as db:
        db.add(SpProduct(id=c.product_id, name=c.key, area="CORE"))
        await db.flush()
        db.add(
            SpApplication(
                id=c.app_id,
                product_id=c.product_id,
                name=c.key,
                product_key=c.key,
                repository="repo",
                runtime="AKS",
                ci="GHA",
            )
        )
        await db.commit()
    yield c
    async with TestSession() as db:
        await db.execute(delete(AuditLog).where(AuditLog.resource_id == str(c.app_id)))
        await db.execute(delete(SpApplication).where(SpApplication.id == c.app_id))
        await db.execute(delete(SpProduct).where(SpProduct.id == c.product_id))
        await db.commit()


async def _seed(c: Ctx, target: str, version: str | None, status: str = "OK") -> None:
    async with TestSession() as db:
        db.add(
            SpVersionState(
                application_id=c.app_id,
                target=target,
                version=version,
                read_status=status,
                error_detail="HTTP 503" if status == "ERROR" else None,
                checked_at=T0,
                last_ok_at=T0 if version else None,
                version_since=T0 if version else None,
            )
        )
        await db.commit()


async def _state(c: Ctx, target: str) -> SpVersionState | None:
    async with TestSession() as db:
        return await db.get(SpVersionState, (c.app_id, target))


async def _events(c: Ctx) -> list[SpDeployEvent]:
    async with TestSession() as db:
        rows = await db.execute(
            select(SpDeployEvent)
            .where(SpDeployEvent.application_id == c.app_id)
            .order_by(SpDeployEvent.detected_at)
        )
        return list(rows.scalars())


async def _audits(c: Ctx) -> list[AuditLog]:
    async with TestSession() as db:
        rows = await db.execute(select(AuditLog).where(AuditLog.resource_id == str(c.app_id)))
        return list(rows.scalars())


async def _run_set(c: Ctx, versions: dict[str, str | None], now: datetime) -> dict:  # type: ignore[type-arg]
    async with TestSession() as db:
        app = await db.get(SpApplication, c.app_id)
        assert app is not None
        changed = await set_manual_versions(db, app, versions, USER, now)
        await db.commit()
        return changed


def test_apply_reading_manual_behaves_like_ok() -> None:
    app_id = uuid.uuid4()
    state, ev = apply_reading(None, Reading("UAT", "MANUAL", "2.0.0", None), T0, app_id)
    assert ev is not None and ev.is_baseline and ev.version == "2.0.0"
    assert state.version_since == T0 and state.last_ok_at == T0
    assert state.read_status == "MANUAL"


def test_manual_targets() -> None:
    assert MANUAL_TARGETS == ("DEV", "UAT", "PROD")


@pytest.mark.asyncio
async def test_make_manual_keeps_last_good_and_creates_missing(ctx: Ctx) -> None:
    await _seed(ctx, "DEV", "1.2.0", status="ERROR")
    async with TestSession() as db:
        app = await db.get(SpApplication, ctx.app_id)
        assert app is not None
        await make_manual(db, app, T1)
        await db.commit()
    dev = await _state(ctx, "DEV")
    assert dev is not None
    assert (dev.version, dev.read_status, dev.error_detail) == ("1.2.0", "MANUAL", None)
    assert dev.last_ok_at == T0 and dev.version_since == T0 and dev.checked_at == T0
    for target in ("UAT", "PROD"):
        row = await _state(ctx, target)
        assert row is not None
        assert (row.version, row.read_status, row.checked_at) == (None, "MANUAL", T1)
        assert row.last_ok_at is None and row.version_since is None
    assert await _events(ctx) == []


@pytest.mark.asyncio
async def test_set_version_change_creates_event(ctx: Ctx) -> None:
    await _seed(ctx, "UAT", "1.2.0", status="MANUAL")
    changed = await _run_set(ctx, {"UAT": " 1.3.0 "}, T1)
    assert changed == {"UAT": ("1.2.0", "1.3.0")}
    events = await _events(ctx)
    assert len(events) == 1
    ev = events[0]
    assert (ev.previous_version, ev.version, ev.previous_seen_at, ev.is_baseline) == (
        "1.2.0",
        "1.3.0",
        T0,
        False,
    )
    state = await _state(ctx, "UAT")
    assert state is not None and state.version == "1.3.0" and state.version_since == T1


@pytest.mark.asyncio
async def test_set_version_from_empty_is_baseline(ctx: Ctx) -> None:
    await _seed(ctx, "UAT", None, status="MANUAL")
    changed = await _run_set(ctx, {"UAT": "1.0.0"}, T1)
    assert changed == {"UAT": (None, "1.0.0")}
    events = await _events(ctx)
    assert len(events) == 1 and events[0].is_baseline and events[0].previous_version is None


@pytest.mark.asyncio
async def test_set_version_creates_missing_row(ctx: Ctx) -> None:
    changed = await _run_set(ctx, {"DEV": "0.1.0"}, T1)
    assert changed == {"DEV": (None, "0.1.0")}
    state = await _state(ctx, "DEV")
    assert state is not None and state.version == "0.1.0" and state.read_status == "MANUAL"


@pytest.mark.asyncio
async def test_same_value_refreshes_without_event(ctx: Ctx) -> None:
    await _seed(ctx, "UAT", "1.2.0", status="MANUAL")
    changed = await _run_set(ctx, {"UAT": "1.2.0"}, T2)
    assert changed == {}
    assert await _events(ctx) == []
    assert await _audits(ctx) == []
    state = await _state(ctx, "UAT")
    assert state is not None
    assert state.checked_at == T2 and state.last_ok_at == T2 and state.version_since == T0


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", ["", None, "   "])
async def test_clear_keeps_row_and_lookup_is_404(ctx: Ctx, empty: str | None) -> None:
    await _seed(ctx, "PROD", "1.2.0", status="MANUAL")
    changed = await _run_set(ctx, {"PROD": empty}, T1)
    assert changed == {"PROD": ("1.2.0", None)}
    state = await _state(ctx, "PROD")
    assert state is not None and state.version is None and state.version_since is None
    assert await _events(ctx) == []
    async with TestSession() as db:
        with pytest.raises(VersionNotFoundError, match="Versione non ancora rilevata"):
            await get_version(db, ctx.key, "PROD")


@pytest.mark.asyncio
async def test_audit_entry(ctx: Ctx) -> None:
    await _seed(ctx, "UAT", "1.2.0", status="MANUAL")
    await _run_set(ctx, {"UAT": "1.3.0"}, T1)
    audits = await _audits(ctx)
    assert len(audits) == 1
    assert audits[0].action == "status_page.application.versions"
    assert audits[0].resource_type == "sp_application"
    assert json.loads(audits[0].detail) == {
        "user": USER,
        "before": {"UAT": "1.2.0"},
        "after": {"UAT": "1.3.0"},
    }


@pytest.mark.asyncio
async def test_invalid_key_and_length_raise(ctx: Ctx) -> None:
    with pytest.raises(ValueError):
        await _run_set(ctx, {"REPO": "1"}, T1)
    with pytest.raises(ValueError):
        await _run_set(ctx, {"UAT": "x" * 101}, T1)


@pytest.mark.asyncio
async def test_manual_versions_of(ctx: Ctx) -> None:
    await _seed(ctx, "DEV", "1.0.0", status="MANUAL")
    await _seed(ctx, "UAT", None, status="MANUAL")
    other = uuid.uuid4()
    async with TestSession() as db:
        result = await manual_versions_of(db, [ctx.app_id, other])
    assert result[ctx.app_id] == {"DEV": "1.0.0", "UAT": None, "PROD": None}
    assert result[other] == {"DEV": None, "UAT": None, "PROD": None}

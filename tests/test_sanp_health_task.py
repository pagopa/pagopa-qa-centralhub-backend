from __future__ import annotations

# ruff: noqa: S101
import importlib
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.core.permissions import ACTION_CATALOG, EDITABLE_ROLES
from app.models.sanp_health import SanpImportStatus
from app.schemas.sanp_health import SanpHealthSyncResult
from app.tasks import celery_app, schedule


def test_sanp_health_permission_has_defaults_for_current_roles() -> None:
    permission = next(
        entry for entry in ACTION_CATALOG if entry["key"] == "view:sanp_health"
    )

    assert permission["defaults"] == {role: role != "guest" for role in EDITABLE_ROLES}


def test_worker_registers_sanp_health_task() -> None:
    assert "app.tasks.sync_sanp_health.sync_sanp_health" in celery_app.tasks


def test_beat_schedules_sanp_health_hourly() -> None:
    assert schedule.celery_app.conf.beat_schedule["sync-sanp-health-hourly"] == {
        "task": "app.tasks.sync_sanp_health.sync_sanp_health",
        "schedule": 3600.0,
    }


@pytest.mark.anyio
async def test_task_invokes_shared_async_service() -> None:
    task_module = importlib.import_module("app.tasks.sync_sanp_health")
    db = object()
    result = SanpHealthSyncResult(
        status=SanpImportStatus.COMPLETE,
        imported_run_count=1,
        errors=[],
    )

    @asynccontextmanager
    async def fake_session():
        yield db

    sync = AsyncMock(return_value=result)
    with (
        patch("app.core.db.async_session", fake_session),
        patch("app.services.sanp_health.sync_from_source", sync),
    ):
        payload = await task_module._async_sync()

    sync.assert_awaited_once_with(db)
    assert payload == result.model_dump(mode="json")


@pytest.mark.anyio
async def test_task_propagates_sync_failures_to_celery() -> None:
    task_module = importlib.import_module("app.tasks.sync_sanp_health")
    db = object()

    @asynccontextmanager
    async def fake_session():
        yield db

    sync = AsyncMock(side_effect=RuntimeError("temporary failure"))
    with (
        patch("app.core.db.async_session", fake_session),
        patch("app.services.sanp_health.sync_from_source", sync),
        pytest.raises(RuntimeError, match="temporary failure"),
    ):
        await task_module._async_sync()
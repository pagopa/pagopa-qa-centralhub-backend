from __future__ import annotations

# ruff: noqa: S101
import importlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.models.status_page import SpSyncStatus
from app.tasks import celery_app, schedule  # noqa: F401

TASK_NAME = "app.tasks.sync_status_page.sync_status_page"


def test_worker_registers_status_page_task() -> None:
    assert TASK_NAME in celery_app.tasks


def test_beat_schedules_status_page_every_15_minutes() -> None:
    assert celery_app.conf.beat_schedule["sync-status-page"] == {
        "task": TASK_NAME,
        "schedule": 900.0,
        "options": {"expires": 900.0},
    }


@pytest.mark.anyio
async def test_task_passes_trigger_and_requested_by_and_returns_json_dict() -> None:
    task_module = importlib.import_module("app.tasks.sync_status_page")
    db = object()
    row = SpSyncStatus(
        id=1,
        status="OK",
        trigger="MANUAL",
        requested_by="qa@example.com",
        queued_at=None,
        started_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        finished_at=datetime(2026, 1, 1, 10, 1, tzinfo=UTC),
        last_success_at=datetime(2026, 1, 1, 10, 1, tzinfo=UTC),
        applications_checked=3,
        reads_failed=0,
        errors=[],
    )

    @asynccontextmanager
    async def fake_session() -> AsyncIterator[object]:
        yield db

    sync = AsyncMock(return_value=row)
    with (
        patch("app.core.db.async_session", fake_session),
        patch("app.services.status_page_sync.run_sync", sync),
    ):
        payload = await task_module._async_sync("MANUAL", "qa@example.com")

    sync.assert_awaited_once_with(db, trigger="MANUAL", requested_by="qa@example.com")
    assert payload["status"] == "OK"
    assert payload["applications_checked"] == 3
    assert payload["started_at"] == "2026-01-01T10:00:00+00:00"
    json.dumps(payload)


@pytest.mark.anyio
async def test_task_propagates_failures_to_celery() -> None:
    task_module = importlib.import_module("app.tasks.sync_status_page")

    @asynccontextmanager
    async def fake_session() -> AsyncIterator[object]:
        yield object()

    sync = AsyncMock(side_effect=RuntimeError("missing token"))
    with (
        patch("app.core.db.async_session", fake_session),
        patch("app.services.status_page_sync.run_sync", sync),
        pytest.raises(RuntimeError, match="missing token"),
    ):
        await task_module._async_sync("SCHEDULED", None)

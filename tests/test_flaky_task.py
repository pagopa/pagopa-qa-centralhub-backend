from __future__ import annotations

# ruff: noqa: S101
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.flaky import FlakyRecomputeInProgressError
from app.tasks import recompute_flaky_scores as task_module


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    session = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr("app.core.db.async_session", MagicMock(return_value=context))
    return session


async def test_task_recomputes_on_schedule(
    monkeypatch: pytest.MonkeyPatch, fake_session: MagicMock
) -> None:
    status = MagicMock(scored_count=12, last_success_at=datetime(2026, 11, 15, 11, 0, tzinfo=UTC))
    recompute = AsyncMock(return_value=status)
    monkeypatch.setattr("app.services.flaky.recompute_scores", recompute)

    result = await task_module._async_recompute()

    recompute.assert_awaited_once_with(fake_session, trigger="SCHEDULE", requested_by=None)
    assert result == {"status": "done", "scored_count": 12}


async def test_task_skips_when_running(
    monkeypatch: pytest.MonkeyPatch, fake_session: MagicMock
) -> None:
    monkeypatch.setattr(
        "app.services.flaky.recompute_scores",
        AsyncMock(side_effect=FlakyRecomputeInProgressError("running")),
    )

    assert await task_module._async_recompute() == {"status": "skipped"}

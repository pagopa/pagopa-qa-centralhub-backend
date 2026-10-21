from __future__ import annotations

# ruff: noqa: S101
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks
from structlog.testing import capture_logs

from app.api.v1.ea import _dispatch
from app.services.ea.sync import engine
from app.services.ea.sync.sources import SourceError
from app.tasks.sync_ea import sync_ea


async def test_contract_failure_emits_trace_before_existing_partial_fallback() -> None:
    run_id = uuid.uuid4()
    state = engine._State({}, {}, {}, set(), {})
    collected = engine._Collected()
    collected.listing[engine.CONTRACTS_REPO] = SimpleNamespace(  # type: ignore[assignment]
        default_branch="main", pushed_at=None
    )
    github = MagicMock()
    github.get_tree = AsyncMock(side_effect=SourceError("secret"))
    with capture_logs() as logs:
        await engine._collect_contracts(state, github, collected, run_id=run_id)

    assert collected.contracts_error is not None
    failed = next(item for item in logs if item["event"] == "ea_sync_phase_failed")
    assert failed["phase"] == "data_contracts_tree"
    assert failed["run_id"] == str(run_id)
    assert failed["error_type"] == "SourceError"
    assert "secret" not in str(logs)


async def test_scheduled_run_links_run_and_task_before_runner_failure() -> None:
    from app.tasks.sync_ea import _async_sync

    run_id = uuid.uuid4()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=MagicMock())
    session.__aexit__ = AsyncMock(return_value=False)
    with (
        patch("app.core.db.async_session", return_value=session),
        patch.object(engine, "create_run", new=AsyncMock(return_value=SimpleNamespace(id=run_id))),
        patch(
            "app.services.ea.sync.runner.run_with_default_sources",
            new=AsyncMock(side_effect=RuntimeError("secret")),
        ),
        capture_logs() as logs,
        pytest.raises(RuntimeError, match="secret"),
    ):
        await _async_sync(None, task_id="scheduled-task")

    created = next(item for item in logs if item["event"] == "ea_sync_run_created")
    assert created["run_id"] == str(run_id)
    assert created["task_id"] == "scheduled-task"


@pytest.mark.parametrize("broker_available", [True, False])
def test_dispatch_traces_delivery_without_logging_exception_message(broker_available: bool) -> None:
    run_id = str(uuid.uuid4())
    background = BackgroundTasks()
    with patch.object(sync_ea, "apply_async") as dispatch, capture_logs() as logs:
        dispatch.return_value.id = "celery-task-id"
        if not broker_available:
            dispatch.side_effect = ConnectionError("redis://secret@private-host")
        _dispatch(run_id, background)

    assert logs[0]["event"] == "ea_sync_dispatch_started"
    expected = "ea_sync_dispatched" if broker_available else "ea_sync_dispatch_inprocess"
    event = next(item for item in logs if item["event"] == expected)
    assert event["run_id"] == run_id
    assert event["duration_ms"] >= 0
    assert "secret" not in str(logs)
    assert "private-host" not in str(logs)
    if broker_available:
        assert event["task_id"] == "celery-task-id"
        assert not background.tasks
    else:
        assert event["error_type"] == "ConnectionError"
        assert len(background.tasks) == 1


@pytest.mark.parametrize(
    "failed_phase", [None, "github_census", "github_tree", "github_files", "confluence_pages"]
)
async def test_source_traces_include_run_and_repository(failed_phase: str | None) -> None:
    run_id = uuid.uuid4()
    repo = SimpleNamespace(name="known", archived=False, default_branch="main", pushed_at=None)
    state = engine._State(
        components={"known": SimpleNamespace(status="active")},  # type: ignore[dict-item]
        product_domains={},
        snapshots={},
        known_teams=set(),
        decided={},
    )
    github = MagicMock()
    github.list_repos = AsyncMock(return_value=[repo])
    github.get_tree = AsyncMock(return_value=SimpleNamespace(sha="sha", paths=[], truncated=False))
    github.get_files = AsyncMock(return_value={})
    confluence = MagicMock()
    confluence.list_pages = AsyncMock(return_value=[])
    operations = {
        "github_census": github.list_repos,
        "github_tree": github.get_tree,
        "github_files": github.get_files,
        "confluence_pages": confluence.list_pages,
    }
    if failed_phase is not None:
        operations[failed_phase].side_effect = SourceError("secret")
    with patch.object(engine, "extract_repo", return_value={}), capture_logs() as logs:
        collected = await engine._collect(
            state, github, confluence, datetime.now(UTC), run_id=run_id
        )

    assert all(item["run_id"] == str(run_id) for item in logs)
    if failed_phase is not None:
        failed = next(item for item in logs if item["event"] == "ea_sync_phase_failed")
        assert failed["phase"] == failed_phase
        assert failed["error_type"] == "SourceError"
        if failed_phase.startswith("github_") and failed_phase != "github_census":
            assert failed["repo"] == "known"
    else:
        assert collected.census_ok
        assert "known" in collected.fresh
        completed = [item["phase"] for item in logs if item["event"] == "ea_sync_phase_completed"]
        assert "data_contracts" in completed
        assert "confluence_pages" in completed
    assert "secret" not in str(logs)


def test_worker_traces_errors_before_the_engine_without_logging_exception_message() -> None:
    run_id = str(uuid.uuid4())
    with (
        patch("app.tasks.sync_ea._async_sync", new=AsyncMock(side_effect=RuntimeError("secret"))),
        capture_logs() as logs,
        pytest.raises(RuntimeError, match="secret"),
    ):
        sync_ea.run(run_id)

    assert logs[0]["event"] == "ea_sync_task_started"
    failed = next(item for item in logs if item["event"] == "ea_sync_task_failed")
    assert failed["run_id"] == run_id
    assert failed["error_type"] == "RuntimeError"
    assert failed["traceback"]
    assert "secret" not in str(logs)


@pytest.mark.parametrize("failed_phase", [None, "read_state", "collect", "apply", "commit"])
async def test_engine_traces_phases_and_failures(failed_phase: str | None) -> None:
    run_id = uuid.uuid4()
    run = SimpleNamespace(id=run_id, status="running", stats={}, error=None)
    db = MagicMock()
    db.get = AsyncMock(return_value=run)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    collected = engine._Collected(census_ok=True)
    github = MagicMock()
    with (
        patch.object(engine, "_read_state", new=AsyncMock()) as read_state,
        patch.object(engine, "_collect", new=AsyncMock(return_value=collected)) as collect,
        patch.object(engine, "_apply", new=AsyncMock(return_value={})) as apply,
        patch.object(engine, "_record_status", new=AsyncMock()),
        patch.object(engine, "_summary", side_effect=lambda value: {"status": value.status}),
        capture_logs() as logs,
    ):
        operations = {"read_state": read_state, "collect": collect, "apply": apply}
        if failed_phase == "commit":
            db.commit.side_effect = [RuntimeError("secret"), None]
        elif failed_phase is not None:
            operations[failed_phase].side_effect = RuntimeError("secret")
        result = await engine.execute_run(db, run_id, github, MagicMock())

    phases = [item["phase"] for item in logs if item["event"] == "ea_sync_phase_started"]
    assert phases[0] == "load_run"
    assert "read_state" in phases
    assert all(item["run_id"] == str(run_id) for item in logs)
    if failed_phase is None:
        assert phases == ["load_run", "read_state", "collect", "apply", "commit"]
        assert result["status"] == "success"
    else:
        failed = next(item for item in logs if item["event"] == "ea_sync_failed")
        assert failed["phase"] == failed_phase
        assert failed["error_type"] == "RuntimeError"
        assert failed["traceback"]
        assert result["status"] == "failed"
    assert "secret" not in str(logs)

from __future__ import annotations

# ruff: noqa: S101
import io
import json
import uuid
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sanp_health import (
    SanpEnvironment,
    SanpHealthChange,
    SanpHealthResult,
    SanpHealthRun,
    SanpHealthSyncStatus,
    SanpImportStatus,
    SanpResultStatus,
)
from app.schemas.sanp_health import SanpHealthSyncResult
from app.services import sanp_health as sanp_health_service
from app.services.sanp_health import (
    ARTIFACT_RETRY_HOURS,
    REPOSITORY,
    RETENTION_DAYS,
    SYNC_LOCK_ID,
    WORKFLOW_FILE,
    SanpHealthSyncInProgressError,
    _collect_run,
    _summarize_errors,
    get_latest_report,
    get_report_detail,
    get_sync_status,
    list_reports,
    sync_from_source,
)
from app.services.sanp_health_artifacts import (
    MAX_ARTIFACT_BYTES,
    DriftArtifactPayload,
    parse_drift_artifact,
)
from tests._db import TestSession

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
TEST_CREDENTIAL = "test-credential"
T = TypeVar("T")


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        await session.execute(delete(SanpHealthChange))
        await session.execute(delete(SanpHealthResult))
        await session.execute(delete(SanpHealthRun))
        await session.execute(delete(SanpHealthSyncStatus))
        await session.commit()
        yield session
        await session.rollback()
        await session.execute(delete(SanpHealthChange))
        await session.execute(delete(SanpHealthResult))
        await session.execute(delete(SanpHealthRun))
        await session.execute(delete(SanpHealthSyncStatus))
        await session.commit()


def _run(
    run_id: int,
    *,
    conclusion: str = "success",
    completed_at: datetime = NOW,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "run_number": run_id,
        "head_sha": f"{run_id:040d}",
        "head_branch": "master",
        "conclusion": conclusion,
        "html_url": f"https://github.com/pagopa/pagopa-api/actions/runs/{run_id}",
        "run_started_at": (completed_at - timedelta(minutes=5)).isoformat(),
        "updated_at": completed_at.isoformat(),
    }


def _artifact(artifact_id: int, name: str) -> dict[str, Any]:
    return {"id": artifact_id, "name": name, "expired": False}


def _zip_payload(
    *,
    spec_name: str = "payments.json",
    env: str = "DEV",
    branch: str = "SANP 3.13.0",
    display_name: str = "Payments SANP",
    description: str = "SANP description",
    changes: list[dict[str, Any]] | None = None,
) -> bytes:
    payload = {
        "file": spec_name,
        "env": env,
        "branch": branch,
        "display_name": display_name,
        "description": description,
        "changes": changes or [],
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("result.json", json.dumps(payload))
    return stream.getvalue()


def _change(level: int, text: str) -> dict[str, Any]:
    return {
        "level": level,
        "id": f"rule-{level}",
        "text": text,
        "path": "/payments",
        "operation": "GET",
        "section": None,
        "comment": None,
    }


def _client(
    runs: list[dict[str, Any]],
    artifacts: dict[int, list[dict[str, Any]]],
    payloads: dict[int, bytes],
) -> AsyncMock:
    client = AsyncMock()
    client.list_workflow_runs.side_effect = lambda _workflow_file, **kwargs: (
        runs if kwargs.get("page", 1) == 1 else []
    )
    client.list_run_artifacts.side_effect = lambda run_id, **kwargs: artifacts.get(run_id, [])[
        (kwargs.get("page", 1) - 1) * kwargs.get("per_page", 100) : kwargs.get("page", 1)
        * kwargs.get("per_page", 100)
    ]
    client.download_artifact.side_effect = lambda artifact_id, **_: payloads[artifact_id]
    return client


async def _sync(db: AsyncSession, client: Any) -> SanpHealthSyncResult:
    with (
        patch("app.services.sanp_health.settings") as mock_settings,
        patch("app.services.sanp_health.GitHubClient", return_value=client) as client_class,
        patch("app.services.sanp_health._utcnow", return_value=NOW),
    ):
        mock_settings.github_token = TEST_CREDENTIAL
        result = await sync_from_source(db)
    client_class.assert_called_once_with(token=TEST_CREDENTIAL, repo=REPOSITORY)
    return result


@pytest.mark.anyio
async def test_sync_rejects_missing_github_token(db: AsyncSession) -> None:
    with (
        patch("app.services.sanp_health.settings") as mock_settings,
        patch("app.services.sanp_health._utcnow", return_value=NOW),
    ):
        mock_settings.github_token = ""
        with pytest.raises(ValueError, match="GITHUB_TOKEN is not configured"):
            await sync_from_source(db)

    status = await db.get(SanpHealthSyncStatus, 1)
    assert status is not None
    assert status.last_attempt_at == NOW
    assert status.last_error == "GitHub token is not configured"
    assert status.imported_run_count == 0


@pytest.mark.anyio
async def test_sync_stops_workflow_pagination_when_ordered_page_crosses_cutoff(
    db: AsyncSession,
) -> None:
    current_runs = [_run(201), _run(200)]
    old_runs = [
        _run(run_id, completed_at=NOW - timedelta(days=RETENTION_DAYS, seconds=1))
        for run_id in range(1, 99)
    ]
    pages = {1: current_runs + old_runs, 2: []}
    client = AsyncMock()
    client.list_workflow_runs.side_effect = lambda _workflow, **kwargs: pages[kwargs["page"]]
    client.list_run_artifacts.return_value = []

    await _sync(db, client)

    assert [call.kwargs["page"] for call in client.list_workflow_runs.await_args_list] == [1]
    assert all(call.kwargs["per_page"] == 100 for call in client.list_workflow_runs.await_args_list)


@pytest.mark.anyio
async def test_sync_paginates_more_than_one_hundred_artifacts(db: AsyncSession) -> None:
    artifacts = [_artifact(index, f"unrelated-{index}") for index in range(1, 101)]
    artifacts.append(_artifact(101, "drift-d-payments"))
    client = _client([_run(202)], {202: artifacts}, {101: _zip_payload()})

    result = await _sync(db, client)

    assert result.status is SanpImportStatus.COMPLETE
    assert [call.kwargs["page"] for call in client.list_run_artifacts.await_args_list] == [1, 2]
    assert all(call.kwargs["per_page"] == 100 for call in client.list_run_artifacts.await_args_list)
    client.download_artifact.assert_awaited_once_with(101, max_bytes=MAX_ARTIFACT_BYTES)


@pytest.mark.anyio
async def test_sync_imports_completed_success_and_failure_runs_within_cutoff(
    db: AsyncSession,
) -> None:
    old_run = _run(9, completed_at=NOW - timedelta(days=RETENTION_DAYS, seconds=1))
    runs = [_run(11, conclusion="success"), _run(10, conclusion="failure"), old_run]
    artifacts = {
        11: [_artifact(111, "drift-d-payments")],
        10: [_artifact(101, "drift-u-payments")],
        9: [_artifact(91, "drift-p-payments")],
    }
    payloads = {
        111: _zip_payload(),
        101: _zip_payload(env="UAT", branch="develop"),
        91: _zip_payload(env="PROD", branch="master"),
    }
    client = _client(runs, artifacts, payloads)

    result = await _sync(db, client)

    assert result.status is SanpImportStatus.COMPLETE
    assert result.imported_run_count == 2
    assert result.errors == []
    client.list_workflow_runs.assert_awaited_once_with(
        WORKFLOW_FILE,
        status="completed",
        per_page=100,
        page=1,
    )
    assert {call.args[0] for call in client.list_run_artifacts.await_args_list} == {10, 11}
    stored = list(await db.scalars(select(SanpHealthRun).order_by(SanpHealthRun.github_run_id)))
    assert [(item.github_run_id, item.conclusion) for item in stored] == [
        (10, "failure"),
        (11, "success"),
    ]


@pytest.mark.anyio
async def test_sync_does_not_retry_runs_past_artifact_retention(db: AsyncSession) -> None:
    expired_run = _run(
        12,
        completed_at=NOW - timedelta(hours=ARTIFACT_RETRY_HOURS, seconds=1),
    )
    client = _client(
        [expired_run],
        {12: [_artifact(121, "drift-d-payments")]},
        {},
    )

    result = await _sync(db, client)

    assert result.status is SanpImportStatus.COMPLETE
    assert result.imported_run_count == 0
    assert result.errors == []
    client.list_run_artifacts.assert_not_awaited()
    status = await db.get(SanpHealthSyncStatus, 1)
    assert status is not None
    assert status.last_error is None


@pytest.mark.anyio
async def test_sync_skips_complete_run_and_retries_partial_run(db: AsyncSession) -> None:
    complete = SanpHealthRun(
        id=uuid.uuid4(),
        github_run_id=20,
        run_number=20,
        head_sha="a" * 40,
        workflow_branch="master",
        conclusion="success",
        html_url="https://example.test/20",
        started_at=NOW - timedelta(minutes=5),
        completed_at=NOW,
        synced_at=NOW,
        import_status=SanpImportStatus.COMPLETE,
    )
    partial = SanpHealthRun(
        id=uuid.uuid4(),
        github_run_id=21,
        run_number=21,
        head_sha="b" * 40,
        workflow_branch="master",
        conclusion="failure",
        html_url="https://example.test/21",
        started_at=NOW - timedelta(minutes=5),
        completed_at=NOW,
        synced_at=NOW,
        import_status=SanpImportStatus.PARTIAL,
        error_message="old error",
    )
    db.add_all([complete, partial])
    await db.commit()
    client = _client(
        [_run(20), _run(21, conclusion="failure")],
        {21: [_artifact(211, "drift-d-payments")]},
        {211: _zip_payload()},
    )

    result = await _sync(db, client)

    assert result.imported_run_count == 1
    client.list_run_artifacts.assert_awaited_once_with(21, per_page=100, page=1)
    await db.refresh(partial)
    assert partial.import_status is SanpImportStatus.COMPLETE
    assert partial.error_message is None


@pytest.mark.anyio
async def test_sync_excludes_main_by_name_and_payload(db: AsyncSession) -> None:
    client = _client(
        [_run(30)],
        {
            30: [
                _artifact(301, "drift-main-payments"),
                _artifact(302, "drift-d-main-payload"),
                _artifact(303, "drift-p-payments"),
            ]
        },
        {
            301: b"must not download",
            302: _zip_payload(env="MAIN", branch="main"),
            303: _zip_payload(env="PROD", branch="master"),
        },
    )

    result = await _sync(db, client)

    assert result.status is SanpImportStatus.COMPLETE
    assert [call.args[0] for call in client.download_artifact.await_args_list] == [302, 303]
    environments = set(await db.scalars(select(SanpHealthResult.environment)))
    assert environments == {SanpEnvironment.PRODUZIONE}


@pytest.mark.anyio
async def test_sync_upserts_result_idempotently_and_replaces_changes(
    db: AsyncSession,
) -> None:
    artifacts = {40: [_artifact(401, "drift-d-payments")]}
    client = _client(
        [_run(40)],
        artifacts,
        {401: _zip_payload(changes=[_change(1, "old info")])},
    )
    first = await _sync(db, client)
    assert first.imported_run_count == 1

    run = await db.scalar(select(SanpHealthRun).where(SanpHealthRun.github_run_id == 40))
    assert run is not None
    run.import_status = SanpImportStatus.PARTIAL
    await db.commit()
    retry_client = _client(
        [_run(40)],
        artifacts,
        {401: _zip_payload(changes=[_change(3, "new error"), _change(2, "new warning")])},
    )

    second = await _sync(db, retry_client)

    assert second.status is SanpImportStatus.COMPLETE
    assert await db.scalar(select(func.count()).select_from(SanpHealthResult)) == 1
    changes = list(
        await db.scalars(select(SanpHealthChange).order_by(SanpHealthChange.change_order))
    )
    assert [(change.level, change.message) for change in changes] == [
        (3, "new error"),
        (2, "new warning"),
    ]
    await db.refresh(run)
    assert (run.error_count, run.warning_count, run.info_count) == (1, 1, 0)


@pytest.mark.anyio
async def test_bad_artifact_marks_partial_but_persists_valid_result(db: AsyncSession) -> None:
    client = _client(
        [_run(50)],
        {50: [_artifact(501, "drift-d-bad"), _artifact(502, "drift-u-valid")]},
        {501: b"not a zip", 502: _zip_payload(env="UAT", branch="develop")},
    )

    result = await _sync(db, client)

    assert result.status is SanpImportStatus.PARTIAL
    assert result.imported_run_count == 1
    assert len(result.errors) == 1
    run = await db.scalar(select(SanpHealthRun).where(SanpHealthRun.github_run_id == 50))
    assert run is not None
    assert run.import_status is SanpImportStatus.PARTIAL
    assert run.error_message and "drift-d-bad" in run.error_message
    environments = list(await db.scalars(select(SanpHealthResult.environment)))
    assert environments == [SanpEnvironment.COLLAUDO]


@pytest.mark.anyio
async def test_expired_artifact_errors_are_compacted(db: AsyncSession) -> None:
    artifacts = [
        {**_artifact(550 + index, f"drift-d-spec-{index}"), "expired": True}
        for index in range(5)
    ]
    client = _client([_run(55)], {55: artifacts}, {})

    result = await _sync(db, client)

    assert result.status is SanpImportStatus.FAILED
    assert result.errors == ["run 55: 5 artifacts expired"]
    run = await db.scalar(select(SanpHealthRun).where(SanpHealthRun.github_run_id == 55))
    assert run is not None
    assert run.error_message == "run 55: 5 artifacts expired"


def test_error_summary_has_an_absolute_limit() -> None:
    errors = [
        f"run {run_id}, artifact drift-d-api: artifact expired"
        for run_id in range(12)
    ]

    summary = _summarize_errors(errors)

    assert len(summary) == 10
    assert summary[-1] == "3 additional errors omitted"


@pytest.mark.anyio
async def test_prefix_environment_mismatch_marks_partial_but_persists_valid_result(
    db: AsyncSession,
) -> None:
    client = _client(
        [_run(52)],
        {52: [_artifact(521, "drift-d-mismatch"), _artifact(522, "drift-u-valid")]},
        {
            521: _zip_payload(spec_name="mismatch.json", env="UAT"),
            522: _zip_payload(spec_name="valid.json", env="UAT"),
        },
    )

    result = await _sync(db, client)

    assert result.status is SanpImportStatus.PARTIAL
    assert result.imported_run_count == 1
    assert result.errors == ["run 52, artifact drift-d-mismatch: invalid artifact"]
    stored = list(await db.scalars(select(SanpHealthResult)))
    assert [(item.spec_name, item.environment) for item in stored] == [
        ("valid.json", SanpEnvironment.COLLAUDO)
    ]


@pytest.mark.anyio
async def test_overlong_artifact_marks_partial_but_persists_valid_result(
    db: AsyncSession,
) -> None:
    client = _client(
        [_run(51)],
        {51: [_artifact(511, "drift-d-overlong"), _artifact(512, "drift-u-valid")]},
        {
            511: _zip_payload(display_name="x" * 501),
            512: _zip_payload(env="UAT", branch="develop"),
        },
    )

    result = await _sync(db, client)

    assert result.status is SanpImportStatus.PARTIAL
    assert result.imported_run_count == 1
    assert len(result.errors) == 1
    stored = list(await db.scalars(select(SanpHealthResult)))
    assert [(item.environment, item.display_name) for item in stored] == [
        (SanpEnvironment.COLLAUDO, "Payments SANP")
    ]


@pytest.mark.anyio
async def test_all_artifacts_failing_marks_run_and_result_failed(db: AsyncSession) -> None:
    client = _client(
        [_run(60)],
        {60: [_artifact(601, "drift-d-bad")]},
        {601: b"not a zip"},
    )

    result = await _sync(db, client)

    assert result.status is SanpImportStatus.FAILED
    run = await db.scalar(select(SanpHealthRun).where(SanpHealthRun.github_run_id == 60))
    assert run is not None
    assert run.import_status is SanpImportStatus.FAILED
    assert await db.scalar(select(func.count()).select_from(SanpHealthResult)) == 0


@pytest.mark.anyio
async def test_global_github_failure_preserves_reports_and_updates_sync_status(
    db: AsyncSession,
) -> None:
    existing = SanpHealthRun(
        id=uuid.uuid4(),
        github_run_id=70,
        run_number=70,
        head_sha="c" * 40,
        workflow_branch="master",
        conclusion="success",
        html_url="https://example.test/70",
        started_at=NOW - timedelta(days=1, minutes=5),
        completed_at=NOW - timedelta(days=1),
        synced_at=NOW - timedelta(days=1),
        import_status=SanpImportStatus.COMPLETE,
    )
    db.add(existing)
    await db.commit()
    client = AsyncMock()
    client.list_workflow_runs.side_effect = httpx.ConnectError("GitHub unavailable")

    with pytest.raises(httpx.ConnectError):
        await _sync(db, client)

    assert await db.scalar(select(func.count()).select_from(SanpHealthRun)) == 1
    status = await db.get(SanpHealthSyncStatus, 1)
    assert status is not None
    assert status.last_attempt_at == NOW
    assert status.last_success_at is None
    assert status.last_error == "GitHub request failed"
    assert status.imported_run_count == 0


@pytest.mark.anyio
async def test_all_artifact_listing_http_failures_are_global_github_failure(
    db: AsyncSession,
) -> None:
    client = _client([_run(74), _run(73)], {}, {})
    client.list_run_artifacts.side_effect = httpx.ConnectError("GitHub unavailable")

    with pytest.raises(httpx.ConnectError, match="GitHub unavailable"):
        await _sync(db, client)

    assert await db.scalar(select(func.count()).select_from(SanpHealthRun)) == 0
    status = await db.get(SanpHealthSyncStatus, 1)
    assert status is not None
    assert status.last_attempt_at == NOW
    assert status.last_success_at is None
    assert status.last_error == "GitHub request failed"
    assert status.imported_run_count == 0


@pytest.mark.anyio
async def test_single_artifact_listing_http_failure_remains_partial(
    db: AsyncSession,
) -> None:
    client = _client(
        [_run(76), _run(75)],
        {75: [_artifact(751, "drift-d-valid")]},
        {751: _zip_payload()},
    )

    async def list_artifacts(run_id: int, **_kwargs: Any) -> list[dict[str, Any]]:
        if run_id == 76:
            raise httpx.ConnectError("one listing failed")
        return [_artifact(751, "drift-d-valid")]

    client.list_run_artifacts.side_effect = list_artifacts

    result = await _sync(db, client)

    assert result.status is SanpImportStatus.PARTIAL
    assert result.imported_run_count == 2
    assert result.errors == ["run 76: artifact listing failed"]
    statuses = dict(
        (await db.execute(select(SanpHealthRun.github_run_id, SanpHealthRun.import_status))).all()
    )
    assert statuses == {
        75: SanpImportStatus.COMPLETE,
        76: SanpImportStatus.FAILED,
    }


@pytest.mark.anyio
async def test_persistence_error_rolls_back_only_failed_run_and_marks_sync_partial(
    db: AsyncSession,
) -> None:
    client = _client(
        [_run(72), _run(71)],
        {
            72: [_artifact(721, "drift-d-first")],
            71: [
                _artifact(711, "drift-u-second"),
                _artifact(712, "drift-p-third"),
            ],
        },
        {
            721: _zip_payload(spec_name="first.json"),
            711: _zip_payload(spec_name="second.json", env="UAT"),
            712: _zip_payload(spec_name="third.json", env="PROD"),
        },
    )
    original_upsert_result = sanp_health_service._upsert_result

    async def fail_on_later_artifact(
        session: AsyncSession,
        run: SanpHealthRun,
        payload: DriftArtifactPayload,
    ) -> None:
        if payload.file == "third.json":
            raise RuntimeError("database detail must stay private")
        await original_upsert_result(session, run, payload)

    with patch(
        "app.services.sanp_health._upsert_result",
        side_effect=fail_on_later_artifact,
    ):
        result = await _sync(db, client)

    assert result.status is SanpImportStatus.PARTIAL
    assert result.imported_run_count == 1
    assert result.errors == ["run 71: persistence failed"]
    runs = list(await db.scalars(select(SanpHealthRun)))
    assert [run.github_run_id for run in runs] == [72]
    stored_results = list(await db.scalars(select(SanpHealthResult)))
    assert [item.spec_name for item in stored_results] == ["first.json"]
    status = await db.get(SanpHealthSyncStatus, 1)
    assert status is not None
    assert status.last_success_at == NOW
    assert status.last_error == "run 71: persistence failed"
    assert "database detail" not in status.last_error
    assert status.imported_run_count == 1


@pytest.mark.anyio
async def test_later_artifact_persistence_error_rolls_back_run_and_marks_sync_failed(
    db: AsyncSession,
) -> None:
    client = _client(
        [_run(77)],
        {77: [_artifact(771, "drift-d-first"), _artifact(772, "drift-u-second")]},
        {
            771: _zip_payload(spec_name="first.json"),
            772: _zip_payload(spec_name="second.json", env="UAT"),
        },
    )
    original_upsert_result = sanp_health_service._upsert_result

    async def fail_on_second_artifact(
        session: AsyncSession,
        run: SanpHealthRun,
        payload: DriftArtifactPayload,
    ) -> None:
        if payload.file == "second.json":
            raise RuntimeError("database detail must stay private")
        await original_upsert_result(session, run, payload)

    with patch(
        "app.services.sanp_health._upsert_result",
        side_effect=fail_on_second_artifact,
    ):
        result = await _sync(db, client)

    assert result.status is SanpImportStatus.FAILED
    assert result.imported_run_count == 0
    assert result.errors == ["run 77: persistence failed"]
    assert await db.scalar(select(func.count()).select_from(SanpHealthRun)) == 0
    status = await db.get(SanpHealthSyncStatus, 1)
    assert status is not None
    assert status.last_success_at is None
    assert status.last_error == "run 77: persistence failed"
    assert status.imported_run_count == 0


@pytest.mark.anyio
async def test_sync_retention_deletes_runs_older_than_seven_days(db: AsyncSession) -> None:
    expired = SanpHealthRun(
        id=uuid.uuid4(),
        github_run_id=80,
        run_number=80,
        head_sha="d" * 40,
        workflow_branch="master",
        conclusion="success",
        html_url="https://example.test/80",
        started_at=NOW - timedelta(days=8, minutes=5),
        completed_at=NOW - timedelta(days=8),
        synced_at=NOW - timedelta(days=8),
        import_status=SanpImportStatus.COMPLETE,
    )
    retained = SanpHealthRun(
        id=uuid.uuid4(),
        github_run_id=81,
        run_number=81,
        head_sha="e" * 40,
        workflow_branch="master",
        conclusion="success",
        html_url="https://example.test/81",
        started_at=NOW - timedelta(days=7, minutes=5),
        completed_at=NOW - timedelta(days=7),
        synced_at=NOW - timedelta(days=7),
        import_status=SanpImportStatus.COMPLETE,
    )
    db.add_all([expired, retained])
    await db.commit()

    await _sync(db, _client([], {}, {}))

    ids = set(await db.scalars(select(SanpHealthRun.github_run_id)))
    assert ids == {81}


@pytest.mark.anyio
async def test_sync_does_not_apply_retention_when_every_imported_run_fails(
    db: AsyncSession,
) -> None:
    expired = SanpHealthRun(
        id=uuid.uuid4(),
        github_run_id=82,
        run_number=82,
        head_sha="8" * 40,
        workflow_branch="master",
        conclusion="success",
        html_url="https://example.test/82",
        started_at=NOW - timedelta(days=8, minutes=5),
        completed_at=NOW - timedelta(days=8),
        synced_at=NOW - timedelta(days=8),
        import_status=SanpImportStatus.COMPLETE,
    )
    db.add(expired)
    await db.commit()
    client = _client(
        [_run(83)],
        {83: [_artifact(831, "drift-d-bad")]},
        {831: b"not a zip"},
    )

    result = await _sync(db, client)

    assert result.status is SanpImportStatus.FAILED
    ids = set(await db.scalars(select(SanpHealthRun.github_run_id)))
    assert ids == {82, 83}


@pytest.mark.anyio
async def test_transaction_advisory_lock_is_held_until_commit_without_unlock(
    db: AsyncSession,
) -> None:
    async with db.begin():
        acquired = await db.scalar(select(func.pg_try_advisory_xact_lock(SYNC_LOCK_ID)))
        assert acquired is True
        async with TestSession() as competing_db:
            with (
                patch("app.services.sanp_health.settings") as mock_settings,
                patch(
                    "app.services.sanp_health.GitHubClient",
                    return_value=_client([], {}, {}),
                ),
                patch("app.services.sanp_health._utcnow", return_value=NOW),
            ):
                mock_settings.github_token = TEST_CREDENTIAL
                with pytest.raises(SanpHealthSyncInProgressError):
                    await sync_from_source(competing_db)

    async with TestSession() as released_db:
        with patch.object(released_db, "scalar", wraps=released_db.scalar) as scalar:
            result = await _sync(released_db, _client([], {}, {}))
    assert result.status is SanpImportStatus.COMPLETE
    statements = [str(call.args[0]) for call in scalar.await_args_list]
    assert any("pg_try_advisory_xact_lock" in statement for statement in statements)
    assert not any("pg_advisory_unlock" in statement for statement in statements)


@pytest.mark.anyio
async def test_all_github_network_and_parsing_finishes_before_database_persistence(
    db: AsyncSession,
) -> None:
    client = AsyncMock()

    def assert_outside_transaction(value: T) -> T:
        assert db.in_transaction() is False
        return value

    client.list_workflow_runs.side_effect = lambda *_args, **kwargs: assert_outside_transaction(
        [_run(84)] if kwargs["page"] == 1 else []
    )
    client.list_run_artifacts.side_effect = lambda *_args, **kwargs: assert_outside_transaction(
        [_artifact(841, "drift-d-payments")] if kwargs["page"] == 1 else []
    )
    client.download_artifact.side_effect = lambda *_args, **_kwargs: assert_outside_transaction(
        _zip_payload()
    )

    def parse_outside_transaction(name: str, content: bytes) -> DriftArtifactPayload | None:
        assert db.in_transaction() is False
        return parse_drift_artifact(name, content)

    with patch(
        "app.services.sanp_health.parse_drift_artifact",
        side_effect=parse_outside_transaction,
    ):
        result = await _sync(db, client)

    assert result.status is SanpImportStatus.COMPLETE
    client.download_artifact.assert_awaited_once()


@pytest.mark.anyio
async def test_sync_skips_collection_completed_by_competitor_before_xact_lock(
    db: AsyncSession,
) -> None:
    original_synced_at = NOW - timedelta(hours=1)
    competing_synced_at = NOW - timedelta(minutes=1)
    run = SanpHealthRun(
        id=uuid.uuid4(),
        github_run_id=85,
        run_number=85,
        head_sha="8" * 40,
        workflow_branch="master",
        conclusion="failure",
        html_url="https://example.test/85",
        started_at=NOW - timedelta(minutes=5),
        completed_at=NOW,
        synced_at=original_synced_at,
        import_status=SanpImportStatus.PARTIAL,
        error_message="old error",
        results=[
            SanpHealthResult(
                spec_name="payments.json",
                environment=SanpEnvironment.SANP,
                source_branch="old branch",
                target_apim="DEV",
                display_name="Old result",
                description="Old description",
                status=SanpResultStatus.INFO,
                error_count=0,
                warning_count=0,
                info_count=1,
                raw_payload={},
            )
        ],
    )
    db.add(run)
    await db.commit()
    client = _client(
        [_run(85)],
        {85: [_artifact(851, "drift-d-payments")]},
        {851: _zip_payload(display_name="Stale collected result")},
    )

    async def complete_in_competing_sync(client: Any, metadata: Any) -> Any:
        collected = await _collect_run(client, metadata)
        async with TestSession() as competing_db:
            competing_run = await competing_db.scalar(
                select(SanpHealthRun).where(SanpHealthRun.github_run_id == 85)
            )
            assert competing_run is not None
            competing_run.import_status = SanpImportStatus.COMPLETE
            competing_run.synced_at = competing_synced_at
            competing_run.error_message = None
            competing_result = await competing_db.scalar(
                select(SanpHealthResult).where(SanpHealthResult.run_id == competing_run.id)
            )
            assert competing_result is not None
            competing_result.display_name = "Competing result"
            await competing_db.commit()
        return collected

    with patch(
        "app.services.sanp_health._collect_run",
        side_effect=complete_in_competing_sync,
    ):
        result = await _sync(db, client)

    assert result.status is SanpImportStatus.COMPLETE
    assert result.imported_run_count == 0
    assert result.errors == []
    await db.refresh(run)
    stored_result = await db.scalar(
        select(SanpHealthResult).where(SanpHealthResult.run_id == run.id)
    )
    assert stored_result is not None
    assert run.import_status is SanpImportStatus.COMPLETE
    assert run.synced_at == competing_synced_at
    assert stored_result.display_name == "Competing result"
    sync_status = await db.get(SanpHealthSyncStatus, 1)
    assert sync_status is not None
    assert sync_status.imported_run_count == 0


async def _add_query_report(db: AsyncSession) -> SanpHealthRun:
    run = SanpHealthRun(
        id=uuid.uuid4(),
        github_run_id=90,
        run_number=90,
        head_sha="f" * 40,
        workflow_branch="master",
        conclusion="failure",
        html_url="https://example.test/90",
        started_at=NOW - timedelta(minutes=5),
        completed_at=NOW,
        synced_at=NOW,
        import_status=SanpImportStatus.COMPLETE,
        error_count=1,
        warning_count=0,
        info_count=0,
    )
    run.results.extend(
        [
            SanpHealthResult(
                spec_name="payments.json",
                environment=SanpEnvironment.SANP,
                source_branch="release / SANP   3.13.0 candidate",
                target_apim="DEV",
                display_name="SANP name",
                description="SANP description",
                status=SanpResultStatus.KO,
                error_count=1,
                warning_count=0,
                info_count=0,
                raw_payload={},
                changes=[
                    SanpHealthChange(
                        level=3,
                        severity="error",
                        rule_id="breaking",
                        message="removed operation",
                        path="/payments",
                        operation="GET",
                        section=None,
                        comment=None,
                        change_order=0,
                    )
                ],
            ),
            SanpHealthResult(
                spec_name="payments.json",
                environment=SanpEnvironment.COLLAUDO,
                source_branch="develop",
                target_apim="UAT",
                display_name="Collaudo name",
                description="Collaudo description",
                status=SanpResultStatus.OK,
                error_count=0,
                warning_count=0,
                info_count=0,
                raw_payload={},
            ),
            SanpHealthResult(
                spec_name="payments.json",
                environment=SanpEnvironment.PRODUZIONE,
                source_branch="master",
                target_apim="PROD",
                display_name="Production name",
                description="Production description",
                status=SanpResultStatus.OK,
                error_count=0,
                warning_count=0,
                info_count=0,
                raw_payload={},
            ),
            SanpHealthResult(
                spec_name="only-sanp.json",
                environment=SanpEnvironment.SANP,
                source_branch="SANP 3.13.0",
                target_apim="DEV",
                display_name="Only SANP",
                description="Only SANP description",
                status=SanpResultStatus.OK,
                error_count=0,
                warning_count=0,
                info_count=0,
                raw_payload={},
            ),
        ]
    )
    db.add(run)
    await db.commit()
    return run


@pytest.mark.anyio
async def test_query_helpers_list_detail_matrix_priority_not_configured_and_regex(
    db: AsyncSession,
) -> None:
    run = await _add_query_report(db)

    reports = await list_reports(db)
    detail = await get_report_detail(db, run.id)
    missing = await get_report_detail(db, uuid.uuid4())

    assert [report.id for report in reports] == [run.id]
    assert missing is None
    assert detail is not None
    assert detail.sanp_version == "3.13.0"
    assert [item.spec_name for item in detail.items] == ["only-sanp.json", "payments.json"]
    payments = detail.items[1]
    assert (payments.display_name, payments.description) == (
        "Production name",
        "Production description",
    )
    assert payments.sanp.changes[0].message == "removed operation"
    only_sanp = detail.items[0]
    assert only_sanp.collaudo.status is SanpResultStatus.NOT_CONFIGURED
    assert only_sanp.produzione.status is SanpResultStatus.NOT_CONFIGURED


@pytest.mark.anyio
async def test_latest_returns_latest_complete_and_marks_stale_after_failed_attempt(
    db: AsyncSession,
) -> None:
    run = await _add_query_report(db)
    db.add(
        SanpHealthRun(
            id=uuid.uuid4(),
            github_run_id=91,
            run_number=91,
            head_sha="1" * 40,
            workflow_branch="master",
            conclusion="failure",
            html_url="https://example.test/91",
            started_at=NOW + timedelta(minutes=5),
            completed_at=NOW + timedelta(minutes=10),
            synced_at=NOW + timedelta(minutes=10),
            import_status=SanpImportStatus.PARTIAL,
            error_message="artifact expired",
        )
    )
    db.add(
        SanpHealthSyncStatus(
            id=1,
            last_attempt_at=NOW + timedelta(minutes=10),
            last_success_at=NOW,
            last_error="artifact expired",
            imported_run_count=1,
        )
    )
    await db.commit()

    latest = await get_latest_report(db)
    status = await get_sync_status(db)

    assert latest.report is not None
    assert latest.report.report.id == run.id
    assert latest.is_stale is True
    assert latest.stale_reason == "artifact expired"
    assert status is not None
    assert status.imported_run_count == 1


@pytest.mark.anyio
async def test_latest_without_complete_report_returns_empty_response(db: AsyncSession) -> None:
    latest = await get_latest_report(db)

    assert latest.report is None
    assert latest.is_stale is False
    assert latest.stale_reason is None


@pytest.mark.anyio
async def test_latest_is_not_stale_when_partial_sync_completed_after_report(
    db: AsyncSession,
) -> None:
    run = await _add_query_report(db)
    db.add(
        SanpHealthSyncStatus(
            id=1,
            last_attempt_at=NOW + timedelta(minutes=10),
            last_success_at=NOW + timedelta(minutes=10),
            last_error="2 artifact scaduti in run storici",
            imported_run_count=1,
        )
    )
    await db.commit()

    latest = await get_latest_report(db)

    assert latest.report is not None
    assert latest.report.report.id == run.id
    assert latest.is_stale is False
    assert latest.stale_reason is None


@pytest.mark.anyio
async def test_latest_ignores_incomplete_runs_past_artifact_retention(
    db: AsyncSession,
) -> None:
    run = await _add_query_report(db)
    run.completed_at = NOW - timedelta(days=2)
    db.add(
        SanpHealthRun(
            id=uuid.uuid4(),
            github_run_id=92,
            run_number=92,
            head_sha="2" * 40,
            workflow_branch="master",
            conclusion="failure",
            html_url="https://example.test/92",
            started_at=NOW - timedelta(hours=26),
            completed_at=NOW - timedelta(hours=25),
            synced_at=NOW - timedelta(hours=25),
            import_status=SanpImportStatus.PARTIAL,
            error_message="artifact expired",
        )
    )
    db.add(
        SanpHealthSyncStatus(
            id=1,
            last_attempt_at=NOW,
            last_success_at=NOW,
            last_error=None,
            imported_run_count=0,
        )
    )
    await db.commit()

    latest = await get_latest_report(db)

    assert latest.report is not None
    assert latest.report.report.id == run.id
    assert latest.is_stale is False
    assert latest.stale_reason is None

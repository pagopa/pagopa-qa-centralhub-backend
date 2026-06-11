from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.gpd_position import GpdPositionSnapshot, GpdPositionSyncStatus
from app.services.gpd_position import (
    get_sync_status,
    list_snapshots,
    parse_report_line,
    sync_from_source,
)

SAMPLE_REPORT = {
    "TOTAL": 275910881,
    "GPD": 110440874,
    "GPD_PAYABLE": 59543593,
    "WISP": 20017656,
    "GPD4ACA": 117079456,
    "GPD4ACA_PAYABLE": 57082760,
    "PA_CREATE_POSITION": 28372895,
    "PA_CREATE_POSITION_PAYABLE": 15027872,
}

SAMPLE_LOG = (
    "2026-06-09T03:14:02.1234567Z report data " + repr(SAMPLE_REPORT) + "\n"
    "2026-06-09T03:14:02.2345678Z creating json report\n"
    "2026-06-09T03:14:02.3456789Z json report created\n"
)


def test_parse_report_line_extracts_dict() -> None:
    result = parse_report_line(SAMPLE_LOG)
    assert result == SAMPLE_REPORT


def test_parse_report_line_returns_none_when_missing() -> None:
    assert parse_report_line("some unrelated log line\nanother line\n") is None


def test_parse_report_line_returns_none_on_invalid_dict() -> None:
    assert parse_report_line("report data {invalid python}\n") is None


SAMPLE_LOG_LINE = "report data " + repr(SAMPLE_REPORT)


@pytest.mark.anyio
async def test_sync_from_source_raises_without_token() -> None:
    db = AsyncMock()

    with patch("app.services.gpd_position.settings") as mock_settings:
        mock_settings.github_token = ""
        with pytest.raises(ValueError):
            await sync_from_source(db)


@pytest.mark.anyio
async def test_sync_from_source_creates_new_snapshot() -> None:
    db = AsyncMock()
    db.scalars.return_value = []
    db.get.return_value = None

    runs = [{"id": 1001, "created_at": "2026-06-09T03:00:00Z"}]

    with (
        patch("app.services.gpd_position.settings") as mock_settings,
        patch("app.services.gpd_position.GitHubClient") as mock_client_cls,
    ):
        mock_settings.github_token = "test-token"
        mock_client = mock_client_cls.return_value
        mock_client.list_workflow_runs = AsyncMock(return_value=runs)
        mock_client.get_job_log = AsyncMock(return_value=SAMPLE_LOG_LINE)

        count = await sync_from_source(db)

    assert count == 1
    added = [call.args[0] for call in db.add.call_args_list]
    snapshot = next(a for a in added if isinstance(a, GpdPositionSnapshot))
    assert snapshot.run_id == 1001
    assert snapshot.report_date == date(2026, 6, 9)
    assert snapshot.total == 275910881
    assert snapshot.pa_create_position_payable == 15027872

    sync_status = next(a for a in added if isinstance(a, GpdPositionSyncStatus))
    assert sync_status.item_count == 1
    db.commit.assert_called_once()


@pytest.mark.anyio
async def test_sync_from_source_stops_at_existing_run_id() -> None:
    db = AsyncMock()
    db.scalars.return_value = [1000]
    db.get.return_value = GpdPositionSyncStatus(id=1, item_count=1)

    runs = [
        {"id": 1001, "created_at": "2026-06-10T03:00:00Z"},
        {"id": 1000, "created_at": "2026-06-09T03:00:00Z"},
    ]

    with (
        patch("app.services.gpd_position.settings") as mock_settings,
        patch("app.services.gpd_position.GitHubClient") as mock_client_cls,
    ):
        mock_settings.github_token = "test-token"
        mock_client = mock_client_cls.return_value
        mock_client.list_workflow_runs = AsyncMock(return_value=runs)
        mock_client.get_job_log = AsyncMock(return_value=SAMPLE_LOG_LINE)

        count = await sync_from_source(db)

    assert count == 1
    mock_client.get_job_log.assert_called_once_with(1001)


@pytest.mark.anyio
async def test_sync_from_source_skips_runs_older_than_backfill_window_on_first_sync() -> None:
    db = AsyncMock()
    db.scalars.return_value = []
    db.get.return_value = None

    old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%dT03:00:00Z")
    runs = [{"id": 2000, "created_at": old_date}]

    with (
        patch("app.services.gpd_position.settings") as mock_settings,
        patch("app.services.gpd_position.GitHubClient") as mock_client_cls,
    ):
        mock_settings.github_token = "test-token"
        mock_client = mock_client_cls.return_value
        mock_client.list_workflow_runs = AsyncMock(return_value=runs)
        mock_client.get_job_log = AsyncMock(return_value=SAMPLE_LOG_LINE)

        count = await sync_from_source(db)

    assert count == 0
    mock_client.get_job_log.assert_not_called()


@pytest.mark.anyio
async def test_sync_from_source_skips_run_without_report_line() -> None:
    db = AsyncMock()
    db.scalars.return_value = []
    db.get.return_value = None

    runs = [{"id": 3000, "created_at": "2026-06-09T03:00:00Z"}]

    with (
        patch("app.services.gpd_position.settings") as mock_settings,
        patch("app.services.gpd_position.GitHubClient") as mock_client_cls,
    ):
        mock_settings.github_token = "test-token"
        mock_client = mock_client_cls.return_value
        mock_client.list_workflow_runs = AsyncMock(return_value=runs)
        mock_client.get_job_log = AsyncMock(return_value="no report here")

        count = await sync_from_source(db)

    assert count == 0


@pytest.mark.anyio
async def test_list_snapshots_returns_scalars() -> None:
    db = AsyncMock()
    db.scalars.return_value = [
        GpdPositionSnapshot(report_date=date(2026, 6, 9), run_id=1, total=1, gpd=1, gpd_payable=1,
                            gpd4aca=1, gpd4aca_payable=1, wisp=1, pa_create_position=1, pa_create_position_payable=1),
    ]

    result = await list_snapshots(db)

    assert len(result) == 1


@pytest.mark.anyio
async def test_get_sync_status_returns_none_when_absent() -> None:
    db = AsyncMock()
    db.get.return_value = None

    result = await get_sync_status(db)

    assert result is None

from __future__ import annotations

# ruff: noqa: S101
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import AsyncClient

from app.models.sanp_health import SanpImportStatus
from app.schemas.sanp_health import SanpHealthLatestResponse, SanpHealthSyncResult
from app.services.sanp_health import SanpHealthSyncInProgressError


@pytest.mark.anyio
async def test_list_reports_returns_items(client: AsyncClient) -> None:
    with patch(
        "app.services.sanp_health.list_reports",
        new_callable=AsyncMock,
        return_value=[],
    ):
        response = await client.get("/api/v1/sanp-health/reports")

    assert response.status_code == 200
    assert response.json() == {"items": []}


@pytest.mark.anyio
async def test_latest_report_route_precedes_uuid_route(client: AsyncClient) -> None:
    latest = SanpHealthLatestResponse(report=None, is_stale=False, stale_reason=None)
    with patch(
        "app.services.sanp_health.get_latest_report",
        new_callable=AsyncMock,
        return_value=latest,
    ):
        response = await client.get("/api/v1/sanp-health/reports/latest")

    assert response.status_code == 200
    assert response.json() == {
        "report": None,
        "is_stale": False,
        "stale_reason": None,
    }


@pytest.mark.anyio
async def test_get_report_returns_404_when_unknown(client: AsyncClient) -> None:
    run_id = uuid.uuid4()
    with patch(
        "app.services.sanp_health.get_report_detail",
        new_callable=AsyncMock,
        return_value=None,
    ):
        response = await client.get(f"/api/v1/sanp-health/reports/{run_id}")

    assert response.status_code == 404
    assert response.json()["detail"] == "SANP Health report not found"


@pytest.mark.anyio
async def test_get_sync_status_returns_null_when_never_run(client: AsyncClient) -> None:
    with patch(
        "app.services.sanp_health.get_sync_status",
        new_callable=AsyncMock,
        return_value=None,
    ):
        response = await client.get("/api/v1/sanp-health/sync-status")

    assert response.status_code == 200
    assert response.json() is None


@pytest.mark.anyio
async def test_trigger_sync_returns_partial_result_details(client: AsyncClient) -> None:
    result = SanpHealthSyncResult(
        status=SanpImportStatus.PARTIAL,
        imported_run_count=2,
        errors=["run 42: artifact listing failed"],
    )
    with patch(
        "app.services.sanp_health.sync_from_source",
        new_callable=AsyncMock,
        return_value=result,
    ):
        response = await client.post("/api/v1/sanp-health/sync")

    assert response.status_code == 200
    assert response.json() == {
        "status": "partial",
        "imported_run_count": 2,
        "errors": ["run 42: artifact listing failed"],
    }


@pytest.mark.anyio
async def test_trigger_sync_returns_503_when_token_is_missing(client: AsyncClient) -> None:
    with patch(
        "app.services.sanp_health.sync_from_source",
        new_callable=AsyncMock,
        side_effect=ValueError("GITHUB_TOKEN is not configured"),
    ):
        response = await client.post("/api/v1/sanp-health/sync")

    assert response.status_code == 503
    assert response.json()["detail"] == "GITHUB_TOKEN is not configured"


@pytest.mark.anyio
async def test_trigger_sync_returns_502_on_github_http_failure(client: AsyncClient) -> None:
    with patch(
        "app.services.sanp_health.sync_from_source",
        new_callable=AsyncMock,
        side_effect=httpx.HTTPError("GitHub unavailable"),
    ):
        response = await client.post("/api/v1/sanp-health/sync")

    assert response.status_code == 502
    assert response.json()["detail"] == "GitHub request failed"


@pytest.mark.anyio
async def test_trigger_sync_returns_409_when_sync_is_running(client: AsyncClient) -> None:
    with patch(
        "app.services.sanp_health.sync_from_source",
        new_callable=AsyncMock,
        side_effect=SanpHealthSyncInProgressError("already running"),
    ):
        response = await client.post("/api/v1/sanp-health/sync")

    assert response.status_code == 409
    assert response.json()["detail"] == "SANP Health synchronization is already running"
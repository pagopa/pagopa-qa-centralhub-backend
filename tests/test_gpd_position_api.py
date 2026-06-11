from __future__ import annotations

import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock, patch


@pytest.mark.anyio
async def test_list_snapshots_returns_items_and_sync_status(client: AsyncClient) -> None:
    with (
        patch(
            "app.api.v1.gpd_position.gpd_position_svc.list_snapshots",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.api.v1.gpd_position.gpd_position_svc.get_sync_status",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        response = await client.get("/api/v1/gpd-position/snapshots")

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["sync_status"] is None


@pytest.mark.anyio
async def test_trigger_sync_returns_item_count(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.gpd_position.gpd_position_svc.sync_from_source",
        new_callable=AsyncMock,
        return_value=5,
    ):
        response = await client.post("/api/v1/gpd-position/sync")

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "item_count": 5}


@pytest.mark.anyio
async def test_trigger_sync_returns_503_when_not_configured(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.gpd_position.gpd_position_svc.sync_from_source",
        new_callable=AsyncMock,
        side_effect=ValueError("GITHUB_TOKEN is not configured"),
    ):
        response = await client.post("/api/v1/gpd-position/sync")

    assert response.status_code == 503
    assert "GITHUB_TOKEN is not configured" in response.json()["detail"]


@pytest.mark.anyio
async def test_trigger_sync_returns_502_on_http_error(client: AsyncClient) -> None:
    import httpx

    with patch(
        "app.api.v1.gpd_position.gpd_position_svc.sync_from_source",
        new_callable=AsyncMock,
        side_effect=httpx.HTTPError("boom"),
    ):
        response = await client.post("/api/v1/gpd-position/sync")

    assert response.status_code == 502

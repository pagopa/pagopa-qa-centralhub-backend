# qa-hub-backend/tests/test_psp_fee_api.py
from __future__ import annotations

import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock, patch


@pytest.mark.anyio
async def test_list_psp_fees_returns_items_and_sync_status(client: AsyncClient) -> None:
    with (
        patch(
            "app.api.v1.psp_fee.psp_fee_svc.list_services",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.api.v1.psp_fee.psp_fee_svc.get_sync_status",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        response = await client.get("/api/v1/psp-fees")

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["sync_status"] is None


@pytest.mark.anyio
async def test_trigger_sync_returns_item_count(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.psp_fee.psp_fee_svc.sync_from_source",
        new_callable=AsyncMock,
        return_value=370,
    ):
        response = await client.post("/api/v1/psp-fees/sync")

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "item_count": 370}


@pytest.mark.anyio
async def test_trigger_sync_returns_502_on_fetch_error(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.psp_fee.psp_fee_svc.sync_from_source",
        new_callable=AsyncMock,
        side_effect=ValueError("PSP fee catalog response has no content"),
    ):
        response = await client.post("/api/v1/psp-fees/sync")

    assert response.status_code == 502
    assert "PSP fee catalog response has no content" in response.json()["detail"]

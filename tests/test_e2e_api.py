from __future__ import annotations

import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock, patch


@pytest.mark.anyio
async def test_list_suites_returns_empty_list(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.e2e.e2e_svc.list_suites_with_latest_run",
        new_callable=AsyncMock,
        return_value=[],
    ):
        response = await client.get("/api/v1/e2e/suites")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.anyio
async def test_get_run_not_found(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.e2e.e2e_svc.get_run",
        new_callable=AsyncMock,
        return_value=None,
    ):
        response = await client.get("/api/v1/e2e/runs/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


@pytest.mark.anyio
async def test_list_runs_returns_paginated(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.e2e.e2e_svc.get_runs",
        new_callable=AsyncMock,
        return_value=([], 0),
    ):
        response = await client.get("/api/v1/e2e/runs")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []

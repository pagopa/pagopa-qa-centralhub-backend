# ruff: noqa: S101

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.anyio
async def test_openapi_spec_is_served_by_a_regular_endpoint(client: AsyncClient) -> None:
    response = await client.get("/api/v1/meta/openapi")
    assert response.status_code == 200
    spec = response.json()
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "QA Hub API"
    assert "/api/v1/health" in spec["paths"]


@pytest.mark.anyio
async def test_the_spec_endpoint_is_part_of_the_spec_itself(client: AsyncClient) -> None:
    """It must be listed, or the gateway import would not expose it."""
    spec = (await client.get("/api/v1/meta/openapi")).json()
    assert "/api/v1/meta/openapi" in spec["paths"]

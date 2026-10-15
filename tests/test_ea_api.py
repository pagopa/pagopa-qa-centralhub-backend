from __future__ import annotations

# ruff: noqa: S101
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

_KB = {"meta": {}, "nodes": [], "edges": [], "blueprint": {}}


@pytest.mark.anyio
async def test_get_kb_returns_payload(client: AsyncClient) -> None:
    with patch("app.services.ea.kb.build_kb", new_callable=AsyncMock, return_value=_KB):
        response = await client.get("/api/v1/ea/kb")
    assert response.status_code == 200
    assert response.json() == _KB


@pytest.mark.anyio
async def test_get_node_returns_404_when_unknown(client: AsyncClient) -> None:
    with patch("app.services.ea.kb.get_node_detail", new_callable=AsyncMock, return_value=None):
        response = await client.get("/api/v1/ea/nodes/unknown")
    assert response.status_code == 404
    assert response.json()["detail"] == "EA node not found"


@pytest.mark.anyio
async def test_impact_validates_depth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/ea/nodes/x/impact?depth=99")
    assert response.status_code == 422

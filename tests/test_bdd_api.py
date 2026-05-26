from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1.bdd import router as bdd_router

_test_app = FastAPI()
_test_app.include_router(bdd_router, prefix="/api/v1/bdd")


@pytest.fixture
async def bdd_client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=_test_app), base_url="http://test") as ac:
        yield ac


async def test_get_settings_returns_defaults(bdd_client: AsyncClient):
    resp = await bdd_client.get("/api/v1/bdd/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ai_provider"] in ("ollama", "claude")
    assert "claude_api_key_set" in data
    assert data["max_scenarios"] >= 1


async def test_create_and_list_project(bdd_client: AsyncClient):
    resp = await bdd_client.post("/api/v1/bdd/projects", json={"name": "Test Project BDD"})
    assert resp.status_code == 201
    project = resp.json()
    assert project["name"] == "Test Project BDD"
    assert project["scenario_count"] == 0

    list_resp = await bdd_client.get("/api/v1/bdd/projects")
    assert list_resp.status_code == 200
    names = [p["name"] for p in list_resp.json()]
    assert "Test Project BDD" in names


async def test_delete_project(bdd_client: AsyncClient):
    resp = await bdd_client.post("/api/v1/bdd/projects", json={"name": "To Delete BDD"})
    assert resp.status_code == 201
    project_id = resp.json()["id"]

    del_resp = await bdd_client.delete(f"/api/v1/bdd/projects/{project_id}")
    assert del_resp.status_code == 204

    get_resp = await bdd_client.get(f"/api/v1/bdd/projects/{project_id}")
    assert get_resp.status_code == 404


async def test_parse_text_endpoint(bdd_client: AsyncClient):
    resp = await bdd_client.post(
        "/api/v1/bdd/parse",
        json={"source_type": "text", "content": "Utente effettua il login con credenziali valide"},
    )
    assert resp.status_code == 200
    assert resp.json()["text"] == "Utente effettua il login con credenziali valide"


from unittest.mock import AsyncMock, MagicMock, patch


async def test_ollama_status_running(bdd_client: AsyncClient):
    with patch("app.api.v1.bdd._probe_ollama", new=AsyncMock(return_value=True)):
        resp = await bdd_client.get("/api/v1/bdd/ollama/status")
    assert resp.status_code == 200
    assert resp.json()["running"] is True


async def test_ollama_status_offline(bdd_client: AsyncClient):
    with patch("app.api.v1.bdd._probe_ollama", new=AsyncMock(return_value=False)):
        resp = await bdd_client.get("/api/v1/bdd/ollama/status")
    assert resp.status_code == 200
    assert resp.json()["running"] is False


async def test_ollama_stop_no_process(bdd_client: AsyncClient):
    with (
        patch("app.api.v1.bdd.psutil.process_iter", return_value=iter([])),
        patch("app.api.v1.bdd._probe_ollama", new=AsyncMock(return_value=False)),
    ):
        resp = await bdd_client.post("/api/v1/bdd/ollama/stop")
    assert resp.status_code == 200
    assert resp.json()["running"] is False

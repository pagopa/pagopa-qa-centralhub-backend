from __future__ import annotations

# ruff: noqa: S101
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.schemas.ea_assistant import AssistantConfig

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


# ── release impact and assistant ─────────────────────────────────────────────


class _NoGitHub:
    async def aclose(self) -> None:
        return None


@pytest.mark.anyio
async def test_release_impact_rejects_bad_links(client: AsyncClient) -> None:
    response = await client.post("/api/v1/ea/impact/release", json={"url": "https://example.com/x"})
    assert response.status_code == 422
    assert "Link non riconosciuto" in response.json()["detail"]


@pytest.mark.anyio
async def test_release_impact_needs_a_github_token(client: AsyncClient) -> None:
    with patch("app.api.v1.ea._github", return_value=None):
        response = await client.post(
            "/api/v1/ea/impact/release",
            json={"url": "https://github.com/pagopa/pagopa-debt-position/pull/1"},
        )
    assert response.status_code == 503


@pytest.mark.anyio
async def test_release_impact_returns_the_analysis(client: AsyncClient) -> None:
    from app.services.ea import release_impact as ri
    from tests.ea_graph_fixture import kb

    cs = ri.ChangeSet(
        repo="pagopa-debt-position",
        title="t",
        url="u",
        ref="PR #7",
        state="open",
        files=[ri.ChangedFile("openapi/openapi.json", "modified")],
    )

    class FakeChanges:
        def __init__(self, _gh: object) -> None: ...

        async def pull_request(self, repo: str, number: int) -> ri.ChangeSet:
            return cs

    with (
        patch("app.api.v1.ea._github", return_value=_NoGitHub()),
        patch("app.services.ea.kb.build_kb", new_callable=AsyncMock, return_value=kb()),
        patch("app.services.ea.release_impact.GitHubChanges", FakeChanges),
    ):
        response = await client.post(
            "/api/v1/ea/impact/release",
            json={"url": "https://github.com/pagopa/pagopa-debt-position/pull/7"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["risk"] == "alto"
    assert body["component"]["id"] == "pagopa-debt-position"


@pytest.mark.anyio
async def test_assistant_unconfigured_returns_503(client: AsyncClient) -> None:
    with (
        patch("app.api.v1.ea.settings.gemini_api_key", ""),
        patch("app.services.ea.assistant_settings.get_config", return_value=AssistantConfig()),
    ):
        response = await client.post(
            "/api/v1/ea/assistant/chat", json={"messages": [{"role": "user", "content": "ciao"}]}
        )
    assert response.status_code == 503
    assert "GEMINI_API_KEY" in response.json()["detail"]


@pytest.mark.anyio
async def test_assistant_chat_answers(client: AsyncClient) -> None:
    from app.services.ea import assistant as a
    from tests.ea_graph_fixture import kb

    class OneShot:
        async def step(self, system, history, tools):  # noqa: ANN001, ANN201
            return a.ModelTurn(text="Ecco [[prod:gpd]].")

    with (
        patch("app.services.ea.assistant_settings.create_model", return_value=OneShot()),
        patch("app.services.ea.assistant_settings.get_config", return_value=AssistantConfig()),
        patch("app.api.v1.ea.settings.gemini_api_key", "test-key"),
        patch("app.api.v1.ea._github", return_value=None),
        patch("app.services.ea.kb.build_kb", new_callable=AsyncMock, return_value=kb()),
    ):
        response = await client.post(
            "/api/v1/ea/assistant/chat",
            json={
                "messages": [{"role": "user", "content": "Cos'è GPD?"}],
                "context": {"view": "map"},
            },
        )
    assert response.status_code == 200
    assert response.json()["highlight"] == ["prod:gpd"]
    assert response.json()["labels"]["prod:gpd"] == "GPD – Posizioni debitorie"


@pytest.mark.anyio
async def test_assistant_validates_messages(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/ea/assistant/chat", json={"messages": [{"role": "system", "content": "x"}]}
    )
    assert response.status_code == 422

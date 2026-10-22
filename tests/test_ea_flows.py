from __future__ import annotations

# ruff: noqa: S101
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.services.ea import flows
from app.services.ea.graph_index import GraphIndex
from tests.ea_graph_fixture import kb

FLOW = {
    "id": "t",
    "name": "Test",
    "subtitle": "s",
    "process": "pr-gpd",
    "services": ["sv-gpd"],
    "lanes": ["pagopa-wisp-converter", "pagopa-debt-position", "ds-gpd", "gone"],
    "steps": [
        {
            "from": "pagopa-wisp-converter",
            "to": "pagopa-debt-position",
            "kind": "sync",
            "primitive": "call",
            "text": "",
        },
        {
            "from": "ds-gpd",
            "to": "pagopa-debt-position",
            "kind": "sync",
            "primitive": "reverse",
            "text": "",
        },
        {
            "from": "pagopa-wisp-converter",
            "to": "ds-gpd",
            "kind": "async",
            "primitive": "nothing",
            "text": "",
        },
        {
            "from": "pagopa-wisp-converter",
            "to": "pagopa-wisp-converter",
            "kind": "user",
            "primitive": "alone",
            "text": "",
        },
        {"from": "gone", "to": "ds-gpd", "kind": "sync", "primitive": "dropped", "text": ""},
    ],
}


def test_flows_are_checked_against_the_map() -> None:
    [f] = flows.flows_for_map(GraphIndex(kb()), [FLOW])
    assert f["missing"] == ["gone"]
    assert [x["id"] for x in f["lanes"]] == [
        "pagopa-wisp-converter",
        "pagopa-debt-position",
        "ds-gpd",
    ]
    assert f["lanes"][0]["product"] == "WISP"
    assert f["process_name"] == "Creazione e pubblicazione posizioni"
    assert f["services"] == [{"id": "sv-gpd", "name": "Posizioni debitorie"}]
    steps = {s["primitive"]: s for s in f["steps"]}
    assert "dropped" not in steps
    assert steps["call"]["on_map"] is True and steps["call"]["edge"]["type"] == "api"
    assert steps["reverse"]["on_map"] is True and steps["reverse"]["edge"]["reverse"] is True
    assert steps["nothing"]["on_map"] is False
    assert steps["alone"]["on_map"] is None
    assert f["validated"] is False


def test_curated_flows_reference_known_blueprint_items() -> None:
    ids = {f["id"] for f in flows.FLOWS}
    assert ids == {"modello-3", "frontend-ec", "checkout", "app-io", "fdr"}
    for f in flows.FLOWS:
        assert all(s["from"] in f["lanes"] and s["to"] in f["lanes"] for s in f["steps"]), f["id"]
        assert all(s["kind"] in ("user", "sync", "async", "self") for s in f["steps"]), f["id"]
        assert all(s["layer"] in ("sanp", "piattaforma") for s in f["steps"]), f["id"]
        assert f["sanp_url"].startswith("https://developer.pagopa.it/"), f["id"]


@pytest.mark.anyio
async def test_flows_endpoint(client: AsyncClient) -> None:
    with patch("app.services.ea.kb.build_kb", new_callable=AsyncMock, return_value=kb()):
        response = await client.get("/api/v1/ea/flows")
    assert response.status_code == 200
    assert [f["id"] for f in response.json()] == [
        "modello-3",
        "frontend-ec",
        "checkout",
        "app-io",
        "fdr",
    ]

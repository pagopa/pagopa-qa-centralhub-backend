from __future__ import annotations

# ruff: noqa: S101
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.services.ea import live
from app.services.ea.graph_index import GraphIndex
from tests.ea_graph_fixture import kb

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _run(
    obj: str,
    hours_ago: float,
    passed: int,
    failed: int = 0,
    broken: int = 0,
    total: int | None = None,
) -> live.RunRow:
    return live.RunRow(
        test_object=obj,
        test_type="NRT",
        trigger="CRON",
        env="UAT",
        started_at=NOW - timedelta(hours=hours_ago),
        total=total if total is not None else passed + failed + broken,
        passed=passed,
        failed=failed,
        broken=broken,
        skipped=0,
        duration_ms=1000,
        allure_url=f"https://allure/{obj}/{hours_ago}",
    )


def test_run_status() -> None:
    assert live.run_status(_run("x", 1, 10)) == "passed"
    assert live.run_status(_run("x", 1, 9, failed=1)) == "failed"
    assert live.run_status(_run("x", 1, 9, broken=1)) == "failed"
    assert live.run_status(_run("x", 1, 5, total=10)) == "partial"


def test_object_nodes_uses_the_qa_signals_of_the_map() -> None:
    g = GraphIndex(kb())
    assert live.object_nodes(g, "wisp") == ["pagopa-wisp-converter"]  # NRT suite
    assert live.object_nodes(g, "integration/gpd") == ["pagopa-debt-position"]  # central suite
    assert live.object_nodes(g, "GPD") == ["pagopa-debt-position"]  # suffix of the central suite
    assert live.object_nodes(g, "pagopa-checkout-fe") == ["pagopa-checkout-fe"]  # component name
    assert live.object_nodes(g, "checkout") == ["prod:checkout"]  # product
    assert live.object_nodes(g, "mystery") == []


def test_summarise_last_outcome_history_and_feed() -> None:
    g = GraphIndex(kb())
    rows = [
        _run("wisp", 2, 9, failed=1),  # latest wisp run failed, 2 hours ago
        _run("wisp", 26, 10),
        _run("wisp", 50, 10),
        _run("gpd", 30, 10),
        _run("mystery", 3, 1),
    ]
    out = live.summarise(g, rows, NOW)
    by = {o["test_object"]: o for o in out["objects"]}
    assert by["wisp"]["status"] == "failed" and by["wisp"]["fresh_failure"] is True
    assert by["wisp"]["history"] == ["passed", "passed", "failed"]
    assert by["wisp"]["pass_rate"] == 0.67
    assert by["wisp"]["last"]["allure_url"] == "https://allure/wisp/2"
    assert out["objects"][0]["test_object"] == "wisp"  # failures first
    assert out["nodes"]["pagopa-wisp-converter"] == {
        "status": "failed",
        "fresh_failure": True,
        "at": by["wisp"]["at"],
        "objects": ["wisp"],
    }
    assert out["nodes"]["pagopa-debt-position"]["status"] == "passed"
    assert out["unmapped"] == ["mystery"]
    assert out["counts"] == {"passed": 2, "partial": 0, "failed": 1}
    assert [f["test_object"] for f in out["feed"]][:2] == ["wisp", "mystery"]
    assert out["feed"][0]["nodes"] == ["pagopa-wisp-converter"]


def test_old_failure_is_not_fresh() -> None:
    out = live.summarise(GraphIndex(kb()), [_run("wisp", 40, 1, failed=1)], NOW)
    assert out["nodes"]["pagopa-wisp-converter"]["fresh_failure"] is False


@pytest.mark.anyio
async def test_live_endpoint(client: AsyncClient) -> None:
    with (
        patch("app.services.ea.kb.build_kb", new_callable=AsyncMock, return_value=kb()),
        patch(
            "app.services.ea.live.load_runs",
            new_callable=AsyncMock,
            return_value=[_run("wisp", 1, 3)],
        ),
    ):
        response = await client.get("/api/v1/ea/live?env=UAT&days=3")
    assert response.status_code == 200
    body = response.json()
    assert body["env"] == "UAT" and body["days"] == 3
    assert body["nodes"]["pagopa-wisp-converter"]["status"] == "passed"


@pytest.mark.anyio
async def test_live_endpoint_validates_env(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/ea/live?env=QA")).status_code == 422


def test_demo_insert_writes_only_the_given_columns() -> None:
    from scripts.ea_live_demo import _insert_sql

    clause = _insert_sql({"id": 1, "env": "DEV", "trigger_type": "CRON", "test_origin": "x"})
    assert str(clause) == (
        "INSERT INTO qachub.test_runs (id, env, trigger_type, test_origin) VALUES "
        "(:id, CAST(:env AS qachub.env_enum), CAST(:trigger_type AS qachub.trigger_type_enum), "
        ":test_origin)"
    )
    assert set(clause.compile().params) == {"id", "env", "trigger_type", "test_origin"}

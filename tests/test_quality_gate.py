# ruff: noqa: S101, S105, S106

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import AsyncClient

from app.config import settings
from app.schemas.quality_gate import QualityGateRunCreate, QualityGateRunOut
from app.services import quality_gate as svc
from app.services.github import GitHubClient


def _fake_db() -> MagicMock:
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tas_github_token", "tok")
    monkeypatch.setattr(settings, "qg_launch_suites", "")


@pytest.mark.anyio
async def test_dispatch_workflow_posts_ref_and_inputs() -> None:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=response) as post:
        await GitHubClient(token="t", repo="org/repo").dispatch_workflow(
            "wf.yml", ref="main", inputs={"a": "b"}
        )
    assert post.call_args.args[0].endswith("/repos/org/repo/actions/workflows/wf.yml/dispatches")
    assert post.call_args.kwargs["json"] == {"ref": "main", "inputs": {"a": "b"}}
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer t"


@pytest.mark.anyio
async def test_launch_sets_quality_gate_flag_and_audits(configured: None) -> None:
    db = _fake_db()
    with patch.object(GitHubClient, "dispatch_workflow", new_callable=AsyncMock) as dispatch:
        out = await svc.launch_quality_gate_run(db, QualityGateRunCreate(test_suite="checkout"))

    inputs = dispatch.call_args.kwargs["inputs"]
    assert inputs["isQualityGateRun"] == "true"
    assert inputs["caller_id"] == "pagopa-qa-centralhub"
    assert inputs["correlation_id"] == out.correlation_id
    assert inputs["allure"] == "true"
    assert out.correlation_id.startswith("qg-")
    assert out.run_name == f"tas-{out.correlation_id}"
    audit = db.add.call_args.args[0]
    assert audit.action == svc.AUDIT_ACTION
    assert audit.resource_id == out.correlation_id
    assert json.loads(audit.detail)["outcome"] == "dispatched"


@pytest.mark.anyio
async def test_launch_without_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tas_github_token", "")
    with pytest.raises(svc.QualityGateConfigError):
        await svc.launch_quality_gate_run(_fake_db(), QualityGateRunCreate(test_suite="checkout"))


@pytest.mark.anyio
async def test_launch_enforces_allowlist(configured: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "qg_launch_suites", "wisp, checkout")
    with patch.object(GitHubClient, "dispatch_workflow", new_callable=AsyncMock) as dispatch:
        with pytest.raises(svc.QualityGateSuiteNotAllowedError):
            await svc.launch_quality_gate_run(_fake_db(), QualityGateRunCreate(test_suite="other"))
        await svc.launch_quality_gate_run(_fake_db(), QualityGateRunCreate(test_suite="wisp"))
    assert dispatch.await_count == 1


@pytest.mark.anyio
async def test_failed_dispatch_is_audited_and_reraised(configured: None) -> None:
    db = _fake_db()
    boom = httpx.ConnectError("x")
    with patch.object(
        GitHubClient, "dispatch_workflow", new_callable=AsyncMock, side_effect=boom
    ):
        with pytest.raises(httpx.ConnectError):
            await svc.launch_quality_gate_run(db, QualityGateRunCreate(test_suite="checkout"))
    assert json.loads(db.add.call_args.args[0].detail)["outcome"] == "failed"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {"test_suite": "../etc"},
        {"test_suite": "Checkout"},
        {"test_suite": "ok", "environment": "prod"},
        {"test_suite": "ok", "test_type": "unit"},
        {"test_suite": "ok", "tags": "@a; rm -rf /"},
    ],
)
async def test_api_rejects_invalid_input(client: AsyncClient, payload: dict) -> None:
    response = await client.post("/api/v1/quality-gate/runs", json=payload)
    assert response.status_code == 422


@pytest.mark.anyio
async def test_api_returns_202_with_run_info(client: AsyncClient) -> None:
    out = QualityGateRunOut(correlation_id="abc", run_name="tas-abc", actions_url="https://x")
    with patch(
        "app.api.v1.quality_gate.quality_gate_svc.launch_quality_gate_run",
        new_callable=AsyncMock,
        return_value=out,
    ):
        response = await client.post("/api/v1/quality-gate/runs", json={"test_suite": "checkout"})
    assert response.status_code == 202
    assert response.json()["run_name"] == "tas-abc"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (svc.QualityGateConfigError("no token"), 503),
        (svc.QualityGateSuiteNotAllowedError("x"), 422),
        (httpx.ConnectError("boom"), 502),
    ],
)
async def test_api_maps_errors(client: AsyncClient, error: Exception, status_code: int) -> None:
    with patch(
        "app.api.v1.quality_gate.quality_gate_svc.launch_quality_gate_run",
        new_callable=AsyncMock,
        side_effect=error,
    ):
        response = await client.post("/api/v1/quality-gate/runs", json={"test_suite": "checkout"})
    assert response.status_code == status_code

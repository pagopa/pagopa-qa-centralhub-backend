# ruff: noqa: S101, S105, S106

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
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


CID = "qg-" + "a" * 32


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("run", "state", "conclusion"),
    [
        (None, "pending", None),
        ({"status": "queued", "conclusion": None}, "queued", None),
        ({"status": "in_progress", "conclusion": None}, "in_progress", None),
        ({"status": "completed", "conclusion": "failure"}, "completed", "failure"),
        ({"status": "completed", "conclusion": "success"}, "completed", "success"),
    ],
)
async def test_run_status_mapping(
    configured: None, run: dict | None, state: str, conclusion: str | None
) -> None:
    with patch.object(
        GitHubClient, "find_workflow_run_by_name", new_callable=AsyncMock, return_value=run
    ) as find:
        out = await svc.get_quality_gate_run_status(CID)
    assert find.call_args.args[1] == f"tas-{CID}"
    assert (out.state, out.conclusion) == (state, conclusion)


@pytest.mark.anyio
async def test_find_workflow_run_by_name_matches_display_title() -> None:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(
        return_value={
            "workflow_runs": [{"display_title": "other"}, {"display_title": "tas-x", "id": 7}]
        }
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response):
        run = await GitHubClient(token="t", repo="o/r").find_workflow_run_by_name("wf.yml", "tas-x")
    assert run == {"display_title": "tas-x", "id": 7}


@pytest.mark.anyio
async def test_run_status_without_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tas_github_token", "")
    with pytest.raises(svc.QualityGateConfigError):
        await svc.get_quality_gate_run_status(CID)


@pytest.mark.anyio
async def test_api_run_status_ok_and_rejects_bad_id(client: AsyncClient) -> None:
    status_out = svc.QualityGateRunStatus(correlation_id=CID, run_name=f"tas-{CID}", state="queued")
    with patch.object(svc, "get_quality_gate_run_status", AsyncMock(return_value=status_out)):
        ok = await client.get(f"/api/v1/quality-gate/runs/{CID}")
        bad = await client.get("/api/v1/quality-gate/runs/not-a-run")
    assert ok.status_code == 200
    assert ok.json()["state"] == "queued"
    assert bad.status_code == 422


@pytest.mark.anyio
async def test_api_run_status_maps_errors(client: AsyncClient) -> None:
    for error, code in [
        (svc.QualityGateConfigError("x"), 503),
        (httpx.ConnectError("boom"), 502),
    ]:
        with patch.object(svc, "get_quality_gate_run_status", AsyncMock(side_effect=error)):
            response = await client.get(f"/api/v1/quality-gate/runs/{CID}")
        assert response.status_code == code


RUN_ID = "11111111-1111-1111-1111-111111111111"


def _run(origin: str | None) -> MagicMock:
    run = MagicMock()
    run.test_origin = origin
    run.suite_id = "s"
    run.timestamp_start = "2026-01-01"
    run.allure_url = "u"
    run.revision_result = None
    return run


def _db_returning(run: MagicMock | None) -> MagicMock:
    db = _fake_db()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=run)
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.anyio
async def test_delete_removes_qg_run_and_audits() -> None:
    db = _db_returning(_run("QG"))
    await svc.delete_quality_gate_run(db, uuid.UUID(RUN_ID))
    assert db.execute.await_count == 2  # lookup + delete
    assert db.add.call_args.args[0].action == svc.DELETE_AUDIT_ACTION
    assert db.add.call_args.args[0].resource_id == RUN_ID


@pytest.mark.anyio
@pytest.mark.parametrize("origin", ["TAS", "NRT", None])
async def test_delete_refuses_non_qg_runs(origin: str | None) -> None:
    db = _db_returning(_run(origin))
    with pytest.raises(svc.NotAQualityGateRunError):
        await svc.delete_quality_gate_run(db, uuid.UUID(RUN_ID))
    assert db.execute.await_count == 1  # lookup only, nothing deleted
    db.add.assert_not_called()


@pytest.mark.anyio
async def test_delete_missing_run() -> None:
    with pytest.raises(svc.QualityGateRunNotFoundError):
        await svc.delete_quality_gate_run(_db_returning(None), uuid.UUID(RUN_ID))


@pytest.mark.anyio
async def test_api_delete_maps_results(client: AsyncClient) -> None:
    for error, code in [
        (None, 204),
        (svc.QualityGateRunNotFoundError("x"), 404),
        (svc.NotAQualityGateRunError("x"), 403),
    ]:
        mock = AsyncMock(side_effect=error)
        with patch.object(svc, "delete_quality_gate_run", mock):
            response = await client.delete(f"/api/v1/quality-gate/test-runs/{RUN_ID}")
        assert response.status_code == code
    assert (await client.delete("/api/v1/quality-gate/test-runs/nope")).status_code == 422


def _gh_run(name: str, status_: str, created: datetime, conclusion: str | None = None) -> dict:
    return {
        "display_title": name,
        "status": status_,
        "conclusion": conclusion,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "html_url": f"https://github.com/x/runs/{name}",
    }


@pytest.mark.anyio
async def test_list_runs_keeps_only_recent_or_active_qg_runs(configured: None) -> None:
    now = datetime.now(UTC)
    runs = [
        _gh_run("tas-qg-" + "a" * 32, "in_progress", now - timedelta(minutes=5)),
        _gh_run("tas-qg-" + "b" * 32, "completed", now - timedelta(minutes=30), "failure"),
        _gh_run("tas-qg-" + "c" * 32, "completed", now - timedelta(hours=3), "success"),
        _gh_run("tas-qg-" + "d" * 32, "queued", now - timedelta(hours=5)),
        _gh_run("tas-" + "e" * 32, "in_progress", now - timedelta(minutes=1)),
        _gh_run("tas-12345", "in_progress", now - timedelta(minutes=2)),
    ]
    with patch.object(
        GitHubClient, "list_dispatched_runs", new_callable=AsyncMock, return_value=runs
    ):
        out = await svc.list_quality_gate_runs()

    assert [r.correlation_id for r in out] == [
        "qg-" + "a" * 32,
        "qg-" + "b" * 32,
        "qg-" + "d" * 32,
    ]
    by_id = {r.correlation_id: r for r in out}
    assert by_id["qg-" + "a" * 32].state == "in_progress"
    assert by_id["qg-" + "b" * 32].conclusion == "failure"
    assert "qg-" + "c" * 32 not in by_id
    assert all(r.run_name.startswith("tas-qg-") for r in out)


@pytest.mark.anyio
async def test_list_runs_without_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tas_github_token", "")
    with pytest.raises(svc.QualityGateConfigError):
        await svc.list_quality_gate_runs()


@pytest.mark.anyio
async def test_api_list_runs_maps_errors(client: AsyncClient) -> None:
    ok = [svc.QualityGateRunStatus(correlation_id=CID, run_name=f"tas-{CID}", state="queued")]
    for result, code in [
        (ok, 200),
        (svc.QualityGateConfigError("x"), 503),
        (httpx.ConnectError("boom"), 502),
    ]:
        mock = (
            AsyncMock(side_effect=result)
            if isinstance(result, Exception)
            else AsyncMock(return_value=result)
        )
        with patch.object(svc, "list_quality_gate_runs", mock):
            response = await client.get("/api/v1/quality-gate/runs")
        assert response.status_code == code

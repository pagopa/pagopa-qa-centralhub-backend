from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import AsyncClient

from app.schemas.quality_gate import NrtLaunchOptionsOut, NrtSuiteOut, QualityGateRunOut
from app.services import quality_gate as svc

BASE = "/api/v1/test-metrics"
HEX = "a" * 32
RUN_ID = "11111111-1111-1111-1111-111111111111"
RUN_OUT = QualityGateRunOut(correlation_id=HEX, run_name=f"x-{HEX}", actions_url="https://x")


async def test_launch_tas_returns_202_and_passes_the_origin(client: AsyncClient) -> None:
    mock = AsyncMock(return_value=RUN_OUT)
    with patch.object(svc, "launch_quality_gate_run", mock):
        response = await client.post(f"{BASE}/tas/runs", json={"test_suite": "checkout"})
    assert response.status_code == 202
    assert response.json()["correlation_id"] == HEX
    assert mock.await_args.args[2] == "TAS"


async def test_launch_nrt_returns_202(client: AsyncClient) -> None:
    mock = AsyncMock(return_value=RUN_OUT)
    with patch.object(svc, "launch_nrt_run", mock):
        response = await client.post(
            f"{BASE}/nrts/runs", json={"suites": ["wisp"], "target_env": "dev"}
        )
        default = await client.post(f"{BASE}/nrts/runs", json={})
    assert response.status_code == 202
    assert mock.await_args_list[0].args[1].suites == ["wisp"]
    assert default.status_code == 202
    assert mock.await_args_list[1].args[1].suites == "all"
    assert mock.await_args_list[1].args[1].target_env == "uat"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("tas/runs", {"test_suite": "../etc"}),
        ("tas/runs", {"test_suite": "ok", "environment": "prod"}),
        ("nrts/runs", {"target_env": "prod"}),
        ("nrts/runs", {"suites": []}),
        ("nrts/runs", {"suites": ["Bad Suite"]}),
        ("nrts/runs", {"suites": "wisp"}),
    ],
)
async def test_launch_rejects_invalid_input(client: AsyncClient, path: str, payload: dict) -> None:
    assert (await client.post(f"{BASE}/{path}", json=payload)).status_code == 422


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (svc.QualityGateConfigError("no token"), 503),
        (svc.QualityGateSuiteNotAllowedError("x"), 422),
        (svc.QualityGateSuiteTypeError("x", "e2e", []), 422),
        (httpx.ConnectError("boom"), 502),
    ],
)
async def test_launch_tas_maps_errors(
    client: AsyncClient, error: Exception, status_code: int
) -> None:
    with patch.object(svc, "launch_quality_gate_run", AsyncMock(side_effect=error)):
        response = await client.post(f"{BASE}/tas/runs", json={"test_suite": "checkout"})
    assert response.status_code == status_code


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (svc.QualityGateConfigError("no token"), 503, "no token"),
        (svc.NrtSuiteSelectionError(["a", "b"], "uat"), 422, "Unknown suite(s): a, b"),
        (svc.NrtSuiteSelectionError([], "dev"), 422, "No suite is configured for 'dev'"),
        (httpx.ConnectError("boom"), 502, "GitHub dispatch failed"),
    ],
)
async def test_launch_nrt_maps_errors(
    client: AsyncClient, error: Exception, status_code: int, detail: str
) -> None:
    with patch.object(svc, "launch_nrt_run", AsyncMock(side_effect=error)):
        response = await client.post(f"{BASE}/nrts/runs", json={})
    assert response.status_code == status_code
    assert response.json()["detail"] == detail


async def test_nrt_launch_options(client: AsyncClient) -> None:
    options = NrtLaunchOptionsOut(
        available=True, suites=[NrtSuiteOut(id="wisp", label="WISP", envs=["dev"])]
    )
    with patch.object(svc, "get_nrt_launch_options", AsyncMock(return_value=options)):
        response = await client.get(f"{BASE}/nrts/launch-options")
    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "suites": [{"id": "wisp", "label": "WISP", "envs": ["dev"]}],
    }


@pytest.mark.parametrize(("segment", "origin"), [("tas", "TAS"), ("nrts", "NRT")])
async def test_status_and_list_use_the_origin(
    client: AsyncClient, segment: str, origin: str
) -> None:
    status_out = svc.QualityGateRunStatus(correlation_id=HEX, run_name="r", state="queued")
    get_status = AsyncMock(return_value=status_out)
    list_runs = AsyncMock(return_value=[status_out])
    with (
        patch.object(svc, "get_quality_gate_run_status", get_status),
        patch.object(svc, "list_quality_gate_runs", list_runs),
    ):
        one = await client.get(f"{BASE}/{segment}/runs/{HEX}")
        many = await client.get(f"{BASE}/{segment}/runs")
        bad = await client.get(f"{BASE}/{segment}/runs/qg-{HEX}")
    assert one.status_code == 200
    assert one.json()["state"] == "queued"
    get_status.assert_awaited_once_with(HEX, origin)
    assert many.status_code == 200
    list_runs.assert_awaited_once_with(origin)
    assert bad.status_code == 422


@pytest.mark.parametrize("segment", ["tas", "nrts"])
async def test_status_and_list_map_errors(client: AsyncClient, segment: str) -> None:
    for error, code in [(svc.QualityGateConfigError("x"), 503), (httpx.ConnectError("b"), 502)]:
        with (
            patch.object(svc, "get_quality_gate_run_status", AsyncMock(side_effect=error)),
            patch.object(svc, "list_quality_gate_runs", AsyncMock(side_effect=error)),
        ):
            assert (await client.get(f"{BASE}/{segment}/runs/{HEX}")).status_code == code
            assert (await client.get(f"{BASE}/{segment}/runs")).status_code == code


@pytest.mark.parametrize(("segment", "origin"), [("tas", "TAS"), ("nrts", "NRT")])
async def test_delete_maps_results_and_passes_the_origin(
    client: AsyncClient, segment: str, origin: str
) -> None:
    for error, code in [
        (None, 204),
        (svc.QualityGateRunNotFoundError("x"), 404),
        (svc.NotAQualityGateRunError("x"), 403),
    ]:
        mock = AsyncMock(side_effect=error)
        with patch.object(svc, "delete_quality_gate_run", mock):
            response = await client.delete(f"{BASE}/{segment}/test-runs/{RUN_ID}")
        assert response.status_code == code
        assert mock.await_args.args[2] == origin
    assert (await client.delete(f"{BASE}/{segment}/test-runs/nope")).status_code == 422

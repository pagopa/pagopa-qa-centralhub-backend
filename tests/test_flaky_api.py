from __future__ import annotations

# ruff: noqa: S101
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.models.flaky import FlakyRecomputeStatus, FlakyRegistry
from app.models.test_metrics import EnvEnum
from app.schemas.common import PaginatedResponse
from app.schemas.flaky import FlakyHistoryItemOut, FlakyScenarioRowOut, FlakySummaryOut
from app.services.flaky import FlakyRecomputeInProgressError

NOW = datetime(2026, 11, 15, 11, 0, tzinfo=UTC)
SVC = "app.services.flaky"


def _row() -> FlakyScenarioRowOut:
    return FlakyScenarioRowOut(
        test_object="checkout",
        test_type="e2e",
        scenario_name="Pay",
        env="UAT",
        score=68,
        level=5,
        state="OK",
        flip_rate=0.54,
        retry_pass_rate=0.25,
        runs_analyzed=28,
        last_failure_at=None,
        recent_outcomes=[],
        last_suite_id=None,
        is_flaky=False,
        flagged_by=None,
        flagged_at=None,
        hint="to_mark",
    )


def _status() -> FlakyRecomputeStatus:
    return FlakyRecomputeStatus(
        id=1,
        last_attempt_at=NOW,
        last_success_at=NOW,
        last_error=None,
        last_trigger="MANUAL",
        last_requested_by="me@x.it",
        scored_count=3,
    )


@pytest.mark.anyio
async def test_list_scenarios_passes_filters(client: AsyncClient) -> None:
    page = PaginatedResponse[FlakyScenarioRowOut](items=[_row()], total=1, page=2, page_size=10)
    with patch(f"{SVC}.list_scenarios", new_callable=AsyncMock, return_value=page) as svc:
        response = await client.get(
            "/api/v1/flaky/scenarios",
            params=[
                ("env", "DEV"),
                ("test_object", "checkout"),
                ("test_object", "gpd"),
                ("flag", "unflagged"),
                ("min_level", "4"),
                ("hint", "to_mark"),
                ("q", "pay"),
                ("page", "2"),
                ("page_size", "10"),
                ("scenario_name", "Pay"),
                ("scenario_name", "Refund"),
            ],
        )

    assert response.status_code == 200
    assert response.json()["items"][0]["hint"] == "to_mark"
    kwargs = svc.await_args.kwargs
    assert kwargs == {
        "env": EnvEnum.DEV,
        "test_objects": ["checkout", "gpd"],
        "flag": "unflagged",
        "min_level": 4,
        "hint": "to_mark",
        "q": "pay",
        "page": 2,
        "page_size": 10,
        "scenario_names": ["Pay", "Refund"],
    }


@pytest.mark.anyio
async def test_list_scenarios_defaults(client: AsyncClient) -> None:
    page = PaginatedResponse[FlakyScenarioRowOut](items=[], total=0, page=1, page_size=50)
    with patch(f"{SVC}.list_scenarios", new_callable=AsyncMock, return_value=page) as svc:
        response = await client.get("/api/v1/flaky/scenarios")

    assert response.status_code == 200
    kwargs = svc.await_args.kwargs
    assert (
        kwargs["env"],
        kwargs["flag"],
        kwargs["test_objects"],
        kwargs["page"],
        kwargs["page_size"],
    ) == (
        EnvEnum.UAT,
        "all",
        None,
        1,
        50,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "params",
    [{"hint": "nope"}, {"flag": "maybe"}, {"min_level": "6"}, {"page_size": "500"}, {"env": "QA"}],
)
async def test_list_scenarios_rejects_invalid_filters(
    client: AsyncClient, params: dict[str, str]
) -> None:
    response = await client.get("/api/v1/flaky/scenarios", params=params)
    assert response.status_code == 422


@pytest.mark.anyio
async def test_summary(client: AsyncClient) -> None:
    summary = FlakySummaryOut(to_mark=7, to_unmark=4, broken=2, flagged=23, analyzed=412)
    with patch(f"{SVC}.get_summary", new_callable=AsyncMock, return_value=summary) as svc:
        response = await client.get("/api/v1/flaky/summary", params={"env": "UAT", "q": "pay"})

    assert response.status_code == 200
    assert response.json() == summary.model_dump()
    assert svc.await_args.kwargs["q"] == "pay"


@pytest.mark.anyio
async def test_history(client: AsyncClient) -> None:
    item = FlakyHistoryItemOut(
        run_id=uuid.uuid4(),
        ts=NOW,
        outcome="F",
        status="FAILED",
        retries=0,
        error_message="boom",
        allure_url=None,
        test_version="1",
        test_object_version="2.0.0",
        test_origin="NRT",
    )
    with patch(f"{SVC}.get_history", new_callable=AsyncMock, return_value=[item]) as svc:
        response = await client.get(
            "/api/v1/flaky/scenarios/history",
            params={
                "env": "UAT",
                "test_object": "checkout",
                "test_type": "e2e",
                "scenario_name": "Pay & go",
            },
        )

    assert response.status_code == 200
    assert response.json()[0]["error_message"] == "boom"
    assert svc.await_args.kwargs == {
        "env": EnvEnum.UAT,
        "test_object": "checkout",
        "test_type": "e2e",
        "scenario_name": "Pay & go",
    }


@pytest.mark.anyio
async def test_set_flag(client: AsyncClient) -> None:
    registry = FlakyRegistry(
        id=uuid.uuid4(),
        test_object="checkout",
        test_type="e2e",
        scenario_name="Pay",
        is_flaky=True,
        flagged_by="me@x.it",
        flagged_at=NOW,
        updated_by="me@x.it",
        updated_at=NOW,
    )
    body = {
        "test_object": "checkout",
        "test_type": "e2e",
        "scenario_name": "Pay",
        "is_flaky": True,
        "user": "me@x.it",
    }
    with patch(f"{SVC}.set_flag", new_callable=AsyncMock, return_value=registry) as svc:
        response = await client.put("/api/v1/flaky/scenarios/flag", json=body)

    assert response.status_code == 200
    assert response.json()["flagged_by"] == "me@x.it"
    assert svc.await_args.args[1].model_dump() == body


@pytest.mark.anyio
async def test_set_flag_requires_user(client: AsyncClient) -> None:
    response = await client.put(
        "/api/v1/flaky/scenarios/flag",
        json={
            "test_object": "checkout",
            "test_type": "e2e",
            "scenario_name": "Pay",
            "is_flaky": True,
        },
    )
    assert response.status_code == 422


@pytest.mark.anyio
async def test_recompute(client: AsyncClient) -> None:
    with patch(f"{SVC}.recompute_scores", new_callable=AsyncMock, return_value=_status()) as svc:
        response = await client.post("/api/v1/flaky/recompute", json={"user": "me@x.it"})

    assert response.status_code == 200
    assert response.json()["scored_count"] == 3
    assert svc.await_args.kwargs == {"trigger": "MANUAL", "requested_by": "me@x.it"}


@pytest.mark.anyio
async def test_recompute_in_progress_returns_409(client: AsyncClient) -> None:
    with patch(
        f"{SVC}.recompute_scores",
        new_callable=AsyncMock,
        side_effect=FlakyRecomputeInProgressError("running"),
    ):
        response = await client.post("/api/v1/flaky/recompute", json={"user": "me@x.it"})

    assert response.status_code == 409
    assert response.json() == {"detail": "Flaky scores recompute is already running"}


@pytest.mark.anyio
async def test_recompute_status(client: AsyncClient) -> None:
    with patch(f"{SVC}.get_recompute_status", new_callable=AsyncMock, return_value=_status()):
        response = await client.get("/api/v1/flaky/recompute/status")
    assert response.status_code == 200
    assert response.json()["last_trigger"] == "MANUAL"

    with patch(f"{SVC}.get_recompute_status", new_callable=AsyncMock, return_value=None):
        response = await client.get("/api/v1/flaky/recompute/status")
    assert response.json() is None

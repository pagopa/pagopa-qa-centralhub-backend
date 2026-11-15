from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.deps import DbDep
from app.models.test_metrics import EnvEnum
from app.schemas.common import PaginatedResponse
from app.schemas.flaky import (
    FlakyFlagFilter,
    FlakyFlagIn,
    FlakyFlagOut,
    FlakyHint,
    FlakyHistoryItemOut,
    FlakyRecomputeIn,
    FlakyRecomputeStatusOut,
    FlakyScenarioRowOut,
    FlakySummaryOut,
)
from app.services import flaky as flaky_svc

router = APIRouter()

EnvQuery = Annotated[EnvEnum, Query()]
TestObjectsQuery = Annotated[list[str] | None, Query(alias="test_object")]
FlagQuery = Annotated[FlakyFlagFilter, Query()]
MinLevelQuery = Annotated[int | None, Query(ge=1, le=5)]
SearchQuery = Annotated[str | None, Query(max_length=200)]


@router.get("/scenarios", response_model=PaginatedResponse[FlakyScenarioRowOut])
async def list_scenarios(
    db: DbDep,
    env: EnvQuery = EnvEnum.UAT,
    test_objects: TestObjectsQuery = None,
    flag: FlagQuery = "all",
    min_level: MinLevelQuery = None,
    hint: Annotated[FlakyHint | None, Query()] = None,
    q: SearchQuery = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    scenario_names: Annotated[list[str] | None, Query(alias="scenario_name")] = None,
) -> PaginatedResponse[FlakyScenarioRowOut]:
    return await flaky_svc.list_scenarios(
        db,
        env=env,
        test_objects=test_objects,
        flag=flag,
        min_level=min_level,
        hint=hint,
        q=q,
        page=page,
        page_size=page_size,
        scenario_names=scenario_names,
    )


@router.get("/summary", response_model=FlakySummaryOut)
async def get_summary(
    db: DbDep,
    env: EnvQuery = EnvEnum.UAT,
    test_objects: TestObjectsQuery = None,
    flag: FlagQuery = "all",
    min_level: MinLevelQuery = None,
    q: SearchQuery = None,
) -> FlakySummaryOut:
    return await flaky_svc.get_summary(
        db, env=env, test_objects=test_objects, flag=flag, min_level=min_level, q=q
    )


@router.get("/scenarios/history", response_model=list[FlakyHistoryItemOut])
async def get_history(
    db: DbDep,
    env: EnvQuery,
    test_object: Annotated[str, Query(min_length=1)],
    test_type: Annotated[str, Query(min_length=1)],
    scenario_name: Annotated[str, Query(min_length=1)],
) -> list[FlakyHistoryItemOut]:
    return await flaky_svc.get_history(
        db, env=env, test_object=test_object, test_type=test_type, scenario_name=scenario_name
    )


@router.put("/scenarios/flag", response_model=FlakyFlagOut)
async def set_flag(db: DbDep, body: FlakyFlagIn) -> FlakyFlagOut:
    registry = await flaky_svc.set_flag(db, body)
    return FlakyFlagOut.model_validate(registry)


@router.post("/recompute", response_model=FlakyRecomputeStatusOut)
async def recompute(db: DbDep, body: FlakyRecomputeIn) -> FlakyRecomputeStatusOut:
    try:
        recompute_status = await flaky_svc.recompute_scores(
            db, trigger="MANUAL", requested_by=body.user
        )
    except flaky_svc.FlakyRecomputeInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Flaky scores recompute is already running",
        ) from exc
    return FlakyRecomputeStatusOut.model_validate(recompute_status)


@router.get("/recompute/status", response_model=FlakyRecomputeStatusOut | None)
async def get_recompute_status(db: DbDep) -> FlakyRecomputeStatusOut | None:
    recompute_status = await flaky_svc.get_recompute_status(db)
    return (
        FlakyRecomputeStatusOut.model_validate(recompute_status)
        if recompute_status is not None
        else None
    )

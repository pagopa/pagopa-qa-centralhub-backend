from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from datetime import datetime, timezone

from app.deps import DbDep
from app.models.test_metrics import TestSuite, TestExecution, TestRun, FlakyScenario
from app.schemas.test_metrics import NrtDetailsObject, TestExecutionCreate, TestExecutionOut, FlakyScenarioIn, FlakyScenarioOut
from app.schemas.test_metrics import TestSuiteOut, TestSuiteCreate, TestRunCreate, TestRunOut, NrtListObject, UpdateNrtsObject, NrtWithLatestRunOut
from app.services import test_metrics as test_metrics_svc
from app.schemas.common import PaginatedResponse


router = APIRouter()

@router.post("/test-suites", response_model=TestSuiteOut)
async def create_test_suite(
    body: TestSuiteCreate,
    db: DbDep,
) -> TestSuiteOut:
    if body is None or (body.test_type == "" or body.test_object == "" or body.suite_version == ""):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body cannot be None",
        )
    model = TestSuite(**body.model_dump())

    try:
        test_suite = await test_metrics_svc.create_test_suite(db, model)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{str(e)}",
        )
    
    if test_suite is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create test suite",
        )
    return test_suite

@router.get("/test-suites/latest/", response_model=TestSuiteOut)
async def get_latest_test_suite_version(
    test_object: str,
    db: DbDep,
) -> TestSuiteOut:
    if not test_object:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query parameter 'test_object' cannot be empty",
        )
    test_suite = await test_metrics_svc.get_latest_test_suite_version(db, test_object)
    if not test_suite:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No test suite found",
        )
    return test_suite

@router.post("/test-runs", response_model=TestRunOut)
async def create_test_run(
    body: TestRunCreate,
    db: DbDep,
    ) -> TestRunOut:
    if body is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body cannot be None",
        )
    model = TestRun(**body.model_dump())
    try:
        test_run_out = await test_metrics_svc.create_test_run(db, model)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{str(e)}",
        )
    if test_run_out is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create test run",
        )
    return test_run_out

@router.post("/test-executions", response_model=list[TestExecutionOut])
async def create_test_execution(
    body: list[TestExecutionCreate],    
    db: DbDep,
    ) -> list[TestExecutionOut]:
    if body is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body cannot be None",
        )
    models = [TestExecution(**te.model_dump()) for te in body]

    try:
        test_executions_out = await test_metrics_svc.create_test_execution(db, models)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{str(e)}",
        )
    if test_executions_out is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create test execution",
        )
    return test_executions_out

@router.get("/nrts", response_model=PaginatedResponse[NrtListObject])
async def get_nrts(db: DbDep, test_object: Annotated[str| None, Query()] = None,
    origin: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
    try:
        nrts, total = await test_metrics_svc.get_nrts(db, test_object, origin, page, page_size)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{str(e)}",
        )
    if nrts is None or len(nrts) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No NRTs found",
            )
    return PaginatedResponse(items=nrts, total=total, page=page, page_size=page_size)

@router.get("/nrt/{id}", response_model=list[TestExecutionOut])
async def get_nrt_by_id(db: DbDep, id: str ):
    try:
        nrt = await test_metrics_svc.get_nrt_by_id(db, id)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{str(e)}",
        )
    if nrt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="NRT not found",
        )
    return nrt

@router.put("/nrts/update", response_model=UpdateNrtsObject)
async def update_nrt(
    body: UpdateNrtsObject,
    db: DbDep,
    ) -> UpdateNrtsObject:
    try:
        updated_nrt = await test_metrics_svc.update_nrt(db, body)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{str(e)}",
        )
    if updated_nrt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="NRT not found",
        )
    return updated_nrt


@router.post("/flaky-scenarios/save", response_model=list[FlakyScenarioOut])
async def save_flaky_scenarios(
    db: DbDep,
    flaky_scenarios: list[FlakyScenarioIn],
) -> list[FlakyScenarioOut]:
    try:
        if flaky_scenarios is not None and len(flaky_scenarios) > 0:
            saved_or_updated_scenarios = await test_metrics_svc.save_or_update_flaky_scenarios(db, flaky_scenarios)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{str(e)}",
        )
    if saved_or_updated_scenarios is None or len(saved_or_updated_scenarios) == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No flaky scenarios saved or updated",
        )
    return saved_or_updated_scenarios


@router.get("/flaky-scenarios", response_model=list[FlakyScenarioOut])
async def get_flaky_scenarios(
    db: DbDep,
    test_object: Annotated[uuid.UUID | None, Query()] = None,
) -> list[FlakyScenarioOut]:
    try:
        scenarios = await test_metrics_svc.get_flaky_scenarios(db, test_object)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{str(e)}",
        )
    if scenarios is None or len(scenarios) == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No flaky scenarios found",
        )
    return scenarios

@router.get("/nrts-suites", response_model=list[NrtWithLatestRunOut])
async def list_suites(db: DbDep,
    origin: Annotated[str | None, Query()] = None) -> list[NrtWithLatestRunOut]:
    pairs = await test_metrics_svc.list_suites_with_latest_run(db, origin)
    return [
        NrtWithLatestRunOut(suite=suite, latest_run=run, trend=trend)
        for suite, run, trend in pairs
    ]

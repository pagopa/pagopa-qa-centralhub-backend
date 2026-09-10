from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel


from app.deps import DbDep
from app.models.test_metrics import TestSuite, TestExecution, TestRun
from app.schemas.test_metrics import TestExecutionCreate, TestExecutionOut, TestSuiteOut, TestSuiteCreate, TestRunCreate, TestRunOut
from app.services import test_metrics as test_metrics_svc

router = APIRouter()

@router.post("/test-suites", response_model=TestSuiteOut)
async def create_test_suite(
    body: TestSuiteCreate,
    db: DbDep,
) -> TestSuiteOut:
    if body is None or (body.test_type is "" or body.test_object is "" or body.suite_version is ""):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body cannot be None",
        )
    model = TestSuite(**body.model_dump())
    test_suite = await test_metrics_svc.create_test_suite(db, model)
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
    test_run_out = await test_metrics_svc.create_test_run(db, model)
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
    test_executions_out = await test_metrics_svc.create_test_execution(db, models)
    if test_executions_out is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create test execution",
        )
    return test_executions_out
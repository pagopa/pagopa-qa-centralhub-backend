from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from app.deps import DbDep
from app.schemas.common import PaginatedResponse
from app.schemas.test_metrics import TestRunOut, TestSuiteOut, TestSuiteCreate
from app.services import test_metrics as test_metrics_svc

router = APIRouter()

@router.post("/test-suites", response_model=TestSuiteOut)
async def create_test_suite(
    body: TestSuiteCreate,
    db: DbDep,
) -> TestSuiteOut:
    if body is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body cannot be None",
        )
    return await test_metrics_svc.create_test_suite(db, body)

@router.get("/test-suites/latest", response_model=TestSuiteOut)
async def get_latest_test_suite_version(
    test_object_in: str,
    db: DbDep,
) -> TestSuiteOut:
    if not test_object_in:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query parameter 'test_object_in' cannot be empty",
        )
    test_suite = await test_metrics_svc.get_latest_test_suite_version(db, test_object_in)
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
    return await test_metrics_svc.create_test_run(db, body)

@router.post("/test-executions", response_model=list[TestExecutionOut])
async def create_test_execution(
    body: list[TestRunCreate],
    db: DbDep,
) -> TestRunOut:
    if body is None:
         raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Request body cannot be None",
        )
    return await test_metrics_svc.create_test_execution(db, body)
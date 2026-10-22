from __future__ import annotations

import uuid
from typing import Annotated


from fastapi import APIRouter, HTTPException, Query, status

from app.deps import DbDep
from app.models.test_metrics import TestSuite, TestExecution, TestRun
from app.schemas.test_metrics import NrtDetailsObject, TestExecutionCreate, TestExecutionOut, TestSuiteOut, TestSuiteCreate, TestRunCreate, TestRunOut, NrtListObject
from app.services import suites as suites_svc

router = APIRouter()

@router.get("/all", response_model=list[str])
async def get_suites(db: DbDep, origin: Annotated[str | None, Query()] = None) -> list[str]:
    return await suites_svc.get_suites(db, origin)
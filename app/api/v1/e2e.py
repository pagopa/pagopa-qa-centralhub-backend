from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.deps import DbDep, require_role
from app.schemas.common import PaginatedResponse
from app.schemas.e2e import RunOut, RunWithSuiteOut, SuiteWithLatestRunOut, SyncResponse
from app.services import e2e as e2e_svc

router = APIRouter()


@router.get("/suites", response_model=list[SuiteWithLatestRunOut])
async def list_suites(db: DbDep) -> list[SuiteWithLatestRunOut]:
    pairs = await e2e_svc.list_suites_with_latest_run(db)
    return [
        SuiteWithLatestRunOut(suite=suite, latest_run=run)
        for suite, run in pairs
    ]


@router.get("/runs", response_model=PaginatedResponse[RunWithSuiteOut])
async def list_runs(
    db: DbDep,
    suite_id: Annotated[uuid.UUID | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> PaginatedResponse[RunWithSuiteOut]:
    rows, total = await e2e_svc.get_runs(db, suite_id=suite_id, page=page, page_size=page_size)
    items = [
        RunWithSuiteOut(
            **RunOut.model_validate(run).model_dump(),
            suite_name=suite_name,
            suite_display_name=suite_display_name,
        )
        for run, suite_name, suite_display_name in rows
    ]
    return PaginatedResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, db: DbDep) -> RunOut:
    run = await e2e_svc.get_run(db, run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


@router.post(
    "/sync",
    response_model=SyncResponse,
    dependencies=[Depends(require_role("qa_lead"))],
)
async def trigger_sync() -> SyncResponse:
    from app.tasks.sync_e2e import sync_e2e_runs

    sync_e2e_runs.delay()
    return SyncResponse(status="queued", message="Sync task enqueued")

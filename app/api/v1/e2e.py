from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from app.deps import DbDep
from app.schemas.common import PaginatedResponse
from app.schemas.e2e import RunOut, RunWithSuiteOut, SuiteCreate, SuiteOut, SuiteUpdate, SuiteWithLatestRunOut, SyncResponse
from app.services import e2e as e2e_svc


class BulkDeleteRequest(BaseModel):
    ids: list[uuid.UUID]

router = APIRouter()


@router.get("/suites", response_model=list[SuiteWithLatestRunOut])
async def list_suites(db: DbDep) -> list[SuiteWithLatestRunOut]:
    pairs = await e2e_svc.list_suites_with_latest_run(db)
    return [
        SuiteWithLatestRunOut(suite=suite, latest_run=run, trend=trend)
        for suite, run, trend in pairs
    ]


@router.post("/suites", response_model=SuiteOut, status_code=status.HTTP_201_CREATED)
async def create_suite(body: SuiteCreate, db: DbDep) -> SuiteOut:
    suite = await e2e_svc.create_suite(
        db,
        name=body.name,
        display_name=body.display_name,
        suite_path=body.suite_path,
        github_repo=body.github_repo,
        enabled=body.enabled,
        sync_lookback_days=body.sync_lookback_days,
    )
    return suite


@router.patch("/suites/{suite_id}", response_model=SuiteOut)
async def update_suite(suite_id: uuid.UUID, body: SuiteUpdate, db: DbDep) -> SuiteOut:
    suite = await e2e_svc.get_suite(db, suite_id)
    if not suite:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Suite not found")
    return await e2e_svc.update_suite(db, suite, body.model_dump(exclude_unset=True))


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


@router.delete("/runs", status_code=status.HTTP_200_OK)
async def delete_runs_bulk(body: BulkDeleteRequest, db: DbDep) -> dict:
    if not body.ids:
        raise HTTPException(status_code=400, detail="No ids provided")
    deleted = await e2e_svc.delete_runs_bulk(db, body.ids)
    return {"deleted": deleted}


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(run_id: uuid.UUID, db: DbDep) -> None:
    run = await e2e_svc.get_run(db, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    await e2e_svc.delete_run(db, run)


@router.post("/sync", response_model=SyncResponse)
async def trigger_sync(db: DbDep) -> SyncResponse:
    from sqlalchemy import select
    from app.models.e2e import E2eSuite

    suites = list(await db.scalars(select(E2eSuite).where(E2eSuite.enabled == True)))  # noqa: E712
    total = 0
    for suite in suites:
        total += await e2e_svc.sync_suite(suite, db)
    return SyncResponse(status="ok", message=f"Sincronizzati {total} nuovi run")

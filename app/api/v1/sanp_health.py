from __future__ import annotations

import uuid

import httpx
from fastapi import APIRouter, HTTPException, status

from app.deps import DbDep
from app.schemas.sanp_health import (
    SanpHealthLatestResponse,
    SanpHealthReportDetailOut,
    SanpHealthReportsResponse,
    SanpHealthRunOut,
    SanpHealthSyncResult,
    SanpHealthSyncStatusOut,
)
from app.services import sanp_health as sanp_health_svc
from app.services.sanp_health import SanpHealthSyncInProgressError

router = APIRouter()


@router.get("/reports", response_model=SanpHealthReportsResponse)
async def list_reports(db: DbDep) -> SanpHealthReportsResponse:
    reports = await sanp_health_svc.list_reports(db)
    return SanpHealthReportsResponse(
        items=[SanpHealthRunOut.model_validate(report) for report in reports]
    )


@router.get("/reports/latest", response_model=SanpHealthLatestResponse)
async def get_latest_report(db: DbDep) -> SanpHealthLatestResponse:
    return await sanp_health_svc.get_latest_report(db)


@router.get("/reports/{run_id}", response_model=SanpHealthReportDetailOut)
async def get_report(run_id: uuid.UUID, db: DbDep) -> SanpHealthReportDetailOut:
    report = await sanp_health_svc.get_report_detail(db, run_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SANP Health report not found",
        )
    return report


@router.post("/sync", response_model=SanpHealthSyncResult)
async def trigger_sync(db: DbDep) -> SanpHealthSyncResult:
    try:
        return await sanp_health_svc.sync_from_source(db)
    except SanpHealthSyncInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="SANP Health synchronization is already running",
        ) from exc
    except ValueError as exc:
        if str(exc) == "GITHUB_TOKEN is not configured":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="GitHub request failed",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="GitHub request failed",
        ) from exc


@router.get("/sync-status", response_model=SanpHealthSyncStatusOut | None)
async def get_sync_status(db: DbDep) -> SanpHealthSyncStatusOut | None:
    sync_status = await sanp_health_svc.get_sync_status(db)
    return (
        SanpHealthSyncStatusOut.model_validate(sync_status)
        if sync_status is not None
        else None
    )
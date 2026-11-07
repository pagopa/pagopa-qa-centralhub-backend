from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Path, status

from app.deps import DbDep
from app.schemas.quality_gate import (
    CORRELATION_ID_PATTERN,
    QualityGateRunCreate,
    QualityGateRunOut,
    QualityGateRunStatus,
)
from app.services import quality_gate as quality_gate_svc

router = APIRouter()


@router.post("/runs", response_model=QualityGateRunOut, status_code=status.HTTP_202_ACCEPTED)
async def launch_run(body: QualityGateRunCreate, db: DbDep) -> QualityGateRunOut:
    try:
        return await quality_gate_svc.launch_quality_gate_run(db, body)
    except quality_gate_svc.QualityGateConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except quality_gate_svc.QualityGateSuiteNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Suite '{exc}' cannot be launched from the Hub",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="GitHub dispatch failed",
        ) from exc


@router.get("/runs/{correlation_id}", response_model=QualityGateRunStatus)
async def get_run_status(
    correlation_id: str = Path(pattern=CORRELATION_ID_PATTERN),
) -> QualityGateRunStatus:
    try:
        return await quality_gate_svc.get_quality_gate_run_status(correlation_id)
    except quality_gate_svc.QualityGateConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="GitHub lookup failed"
        ) from exc

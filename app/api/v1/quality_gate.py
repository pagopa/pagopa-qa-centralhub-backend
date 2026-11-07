from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, status

from app.deps import DbDep
from app.schemas.quality_gate import QualityGateRunCreate, QualityGateRunOut
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

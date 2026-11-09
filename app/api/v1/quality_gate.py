from __future__ import annotations

import uuid

import httpx
from fastapi import APIRouter, HTTPException, Path, status

from app.deps import DbDep
from app.schemas.quality_gate import (
    CORRELATION_ID_PATTERN,
    QualityGateRunCreate,
    QualityGateRunOut,
    QualityGateRunStatus,
    QualityGateScenarioOut,
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


@router.get("/runs", response_model=list[QualityGateRunStatus])
async def list_runs() -> list[QualityGateRunStatus]:
    try:
        return await quality_gate_svc.list_quality_gate_runs()
    except quality_gate_svc.QualityGateConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="GitHub lookup failed"
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


@router.delete("/test-runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(run_id: uuid.UUID, db: DbDep) -> None:
    try:
        await quality_gate_svc.delete_quality_gate_run(db, run_id)
    except quality_gate_svc.QualityGateRunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found") from exc
    except quality_gate_svc.NotAQualityGateRunError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Quality Gate runs can be deleted",
        ) from exc


@router.get("/test-runs/{run_id}/scenarios", response_model=list[QualityGateScenarioOut])
async def list_run_scenarios(run_id: uuid.UUID, db: DbDep) -> list[QualityGateScenarioOut]:
    try:
        scenarios = await quality_gate_svc.list_quality_gate_run_scenarios(db, run_id)
    except quality_gate_svc.QualityGateRunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found") from exc
    except quality_gate_svc.NotAQualityGateRunError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Quality Gate runs have a report",
        ) from exc
    return [QualityGateScenarioOut.model_validate(s) for s in scenarios]

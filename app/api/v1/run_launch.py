"""Launch, follow and delete the TAS and NRT runs of the Hub.

Mounted under ``/test-metrics`` next to the TAS and NRT report routes. The Quality Gate has the
same operations under ``/quality-gate`` (``app/api/v1/quality_gate.py``); the service functions
are shared and take the origin of the runs.
"""

from __future__ import annotations

import uuid

import httpx
from fastapi import APIRouter, HTTPException, Path, status

from app.deps import DbDep
from app.schemas.quality_gate import (
    NrtLaunchOptionsOut,
    NrtRunCreate,
    QualityGateRunCreate,
    QualityGateRunOut,
    QualityGateRunStatus,
)
from app.services import quality_gate as quality_gate_svc

router = APIRouter()

# Correlation id of a Hub-launched TAS or NRT run (the QG ids carry a ``qg-`` prefix).
CORRELATION_ID_PATTERN = r"^[0-9a-f]{32}$"


def _config_error(exc: quality_gate_svc.QualityGateConfigError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


def _github_error(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


def _add_origin_routes(segment: str, origin: str) -> None:
    """Status, list and delete routes of one origin (``/tas/...`` or ``/nrts/...``)."""

    @router.get(f"/{segment}/runs", response_model=list[QualityGateRunStatus])
    async def list_runs() -> list[QualityGateRunStatus]:
        try:
            return await quality_gate_svc.list_quality_gate_runs(origin)
        except quality_gate_svc.QualityGateConfigError as exc:
            raise _config_error(exc) from exc
        except httpx.HTTPError as exc:
            raise _github_error("GitHub lookup failed") from exc

    @router.get(f"/{segment}/runs/{{correlation_id}}", response_model=QualityGateRunStatus)
    async def get_run_status(
        correlation_id: str = Path(pattern=CORRELATION_ID_PATTERN),
    ) -> QualityGateRunStatus:
        try:
            return await quality_gate_svc.get_quality_gate_run_status(correlation_id, origin)
        except quality_gate_svc.QualityGateConfigError as exc:
            raise _config_error(exc) from exc
        except httpx.HTTPError as exc:
            raise _github_error("GitHub lookup failed") from exc

    @router.delete(f"/{segment}/test-runs/{{run_id}}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_run(run_id: uuid.UUID, db: DbDep) -> None:
        try:
            await quality_gate_svc.delete_quality_gate_run(db, run_id, origin)
        except quality_gate_svc.QualityGateRunNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Run not found"
            ) from exc
        except quality_gate_svc.NotAQualityGateRunError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"Not a {origin} run"
            ) from exc


_add_origin_routes("tas", "TAS")
_add_origin_routes("nrts", "NRT")


@router.post("/tas/runs", response_model=QualityGateRunOut, status_code=status.HTTP_202_ACCEPTED)
async def launch_tas_run(body: QualityGateRunCreate, db: DbDep) -> QualityGateRunOut:
    try:
        return await quality_gate_svc.launch_quality_gate_run(db, body, "TAS")
    except quality_gate_svc.QualityGateConfigError as exc:
        raise _config_error(exc) from exc
    except quality_gate_svc.QualityGateSuiteNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Suite '{exc}' cannot be launched from the Hub",
        ) from exc
    except quality_gate_svc.QualityGateSuiteTypeError as exc:
        if exc.available:
            detail = (
                f"Suite '{exc.suite}' has no '{exc.test_type}' tests "
                f"(available: {', '.join(exc.available)})"
            )
        else:
            detail = f"Suite '{exc.suite}' does not exist in the TAS repository"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
        ) from exc
    except httpx.HTTPError as exc:
        raise _github_error("GitHub dispatch failed") from exc


@router.get("/nrts/launch-options", response_model=NrtLaunchOptionsOut)
async def get_nrt_launch_options() -> NrtLaunchOptionsOut:
    return await quality_gate_svc.get_nrt_launch_options()


@router.post("/nrts/runs", response_model=QualityGateRunOut, status_code=status.HTTP_202_ACCEPTED)
async def launch_nrt_run(body: NrtRunCreate, db: DbDep) -> QualityGateRunOut:
    try:
        return await quality_gate_svc.launch_nrt_run(db, body)
    except quality_gate_svc.QualityGateConfigError as exc:
        raise _config_error(exc) from exc
    except quality_gate_svc.NrtSuiteSelectionError as exc:
        if exc.unknown:
            detail = f"Unknown suite(s): {', '.join(exc.unknown)}"
        else:
            detail = f"No suite is configured for '{exc.target_env}'"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
        ) from exc
    except httpx.HTTPError as exc:
        raise _github_error("GitHub dispatch failed") from exc

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.deps import DbDep
from app.schemas.tm import (
    CsvAbsenceImportRequest,
    CsvAbsenceImportResult,
    CostReport,
    ExternalResourceCreate,
    ExternalResourceOut,
    ExternalResourceUpdate,
    ResourceAbsenceCreate,
    ResourceAbsenceOut,
    SyncResult,
)
from app.services import tm as tm_svc

router = APIRouter()


# ── Resources ─────────────────────────────────────────────────────────────────

@router.get("/resources", response_model=list[ExternalResourceOut])
async def list_resources(
    db: DbDep,
    include_inactive: bool = Query(False),
) -> list[ExternalResourceOut]:
    resources = await tm_svc.list_resources(db, include_inactive=include_inactive)
    return [ExternalResourceOut.model_validate(r) for r in resources]


@router.post("/resources", response_model=ExternalResourceOut, status_code=status.HTTP_201_CREATED)
async def create_resource(
    body: ExternalResourceCreate,
    db: DbDep,
) -> ExternalResourceOut:
    existing = await tm_svc.get_resource_by_email(db, str(body.email))
    if existing:
        raise HTTPException(status_code=409, detail="A resource with this email already exists")
    resource = await tm_svc.create_resource(db, body.model_dump())
    return ExternalResourceOut.model_validate(resource)


@router.patch("/resources/{resource_id}", response_model=ExternalResourceOut)
async def update_resource(
    resource_id: uuid.UUID,
    body: ExternalResourceUpdate,
    db: DbDep,
) -> ExternalResourceOut:
    resource = await tm_svc.get_resource(db, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    updates = body.model_dump(exclude_unset=True)
    if "email" in updates:
        conflict = await tm_svc.get_resource_by_email(db, updates["email"])
        if conflict and conflict.id != resource_id:
            raise HTTPException(status_code=409, detail="Email already in use by another resource")
    resource = await tm_svc.update_resource(db, resource, updates)
    return ExternalResourceOut.model_validate(resource)


@router.delete("/resources/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_resource(
    resource_id: uuid.UUID,
    db: DbDep,
) -> None:
    resource = await tm_svc.get_resource(db, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    await tm_svc.update_resource(db, resource, {"is_active": False})


# ── Absences ──────────────────────────────────────────────────────────────────

@router.get("/absences", response_model=list[ResourceAbsenceOut])
async def list_absences(
    db: DbDep,
    resource_id: uuid.UUID | None = Query(None),
    year: int | None = Query(None),
    month: int | None = Query(None),
) -> list[ResourceAbsenceOut]:
    absences = await tm_svc.list_absences(db, resource_id=resource_id, year=year, month=month)
    return [ResourceAbsenceOut.model_validate(a) for a in absences]


@router.post("/absences", response_model=ResourceAbsenceOut, status_code=status.HTTP_201_CREATED)
async def create_absence(
    body: ResourceAbsenceCreate,
    db: DbDep,
) -> ResourceAbsenceOut:
    resource = await tm_svc.get_resource(db, body.resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    absence = await tm_svc.create_absence(
        db,
        {
            "resource_id": body.resource_id,
            "absence_date": body.absence_date,
            "absence_type": body.absence_type,
            "source": "manual",
            "note": body.note,
        },
    )
    return ResourceAbsenceOut.model_validate(absence)


@router.delete("/absences/{absence_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_absence(
    absence_id: uuid.UUID,
    db: DbDep,
) -> None:
    await tm_svc.delete_absence(db, absence_id)


@router.post("/absences/import-csv", response_model=CsvAbsenceImportResult)
async def import_absences_csv(
    body: CsvAbsenceImportRequest,
    db: DbDep,
) -> CsvAbsenceImportResult:
    result = await tm_svc.import_absences_rows(
        db,
        [row.model_dump() for row in body.rows],
    )
    return CsvAbsenceImportResult(**result)


# ── Confluence sync ───────────────────────────────────────────────────────────

@router.post("/absences/sync-confluence", response_model=SyncResult)
async def sync_confluence(
    db: DbDep,
    year: int = Query(...),
    month: int = Query(...),
) -> SyncResult:
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="month must be between 1 and 12")
    result = await tm_svc.sync_from_confluence(db, year=year, month=month)
    return SyncResult(**result)


# ── Cost report ───────────────────────────────────────────────────────────────

@router.get("/costs", response_model=CostReport)
async def get_cost_report(
    db: DbDep,
    year: int = Query(...),
    month: int = Query(...),
) -> CostReport:
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="month must be between 1 and 12")
    report = await tm_svc.compute_cost_report(db, year=year, month=month)
    return CostReport(**report)

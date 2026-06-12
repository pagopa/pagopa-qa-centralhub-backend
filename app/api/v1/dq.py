from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.deps import DbDep
from app.models.dq import DqCategory
from app.schemas.dq import (
    DqCatalogControlCreate,
    DqCatalogControlOut,
    DqCatalogControlUpdate,
    DqControlInstanceCreate,
    DqControlInstanceOut,
    DqControlInstanceUpdate,
    DqDimensionCreate,
    DqDimensionOut,
    DqDimensionUpdate,
    DqDomainCreate,
    DqDomainOut,
    DqDomainUpdate,
)
from app.services import dq as dq_svc

router = APIRouter()


# ── Dimensions ────────────────────────────────────────────────────────────────

@router.get("/dimensions", response_model=list[DqDimensionOut])
async def list_dimensions(db: DbDep) -> list[DqDimensionOut]:
    dimensions = await dq_svc.list_dimensions(db)
    return [DqDimensionOut.model_validate(d) for d in dimensions]


@router.post("/dimensions", response_model=DqDimensionOut, status_code=status.HTTP_201_CREATED)
async def create_dimension(body: DqDimensionCreate, db: DbDep) -> DqDimensionOut:
    dimension = await dq_svc.create_dimension(db, name=body.name, sort_order=body.sort_order)
    return DqDimensionOut.model_validate(dimension)


@router.patch("/dimensions/{dimension_id}", response_model=DqDimensionOut)
async def update_dimension(
    dimension_id: uuid.UUID, body: DqDimensionUpdate, db: DbDep
) -> DqDimensionOut:
    dimension = await dq_svc.get_dimension(db, dimension_id)
    if not dimension:
        raise HTTPException(status_code=404, detail="Dimension not found")
    dimension = await dq_svc.update_dimension(db, dimension, body.model_dump(exclude_unset=True))
    return DqDimensionOut.model_validate(dimension)


@router.delete("/dimensions/{dimension_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dimension(dimension_id: uuid.UUID, db: DbDep) -> None:
    dimension = await dq_svc.get_dimension(db, dimension_id)
    if not dimension:
        raise HTTPException(status_code=404, detail="Dimension not found")
    await dq_svc.delete_dimension(db, dimension)


# ── Domains ───────────────────────────────────────────────────────────────────

@router.get("/domains", response_model=list[DqDomainOut])
async def list_domains(db: DbDep) -> list[DqDomainOut]:
    domains = await dq_svc.list_domains(db)
    return [DqDomainOut.model_validate(d) for d in domains]


@router.post("/domains", response_model=DqDomainOut, status_code=status.HTTP_201_CREATED)
async def create_domain(body: DqDomainCreate, db: DbDep) -> DqDomainOut:
    domain = await dq_svc.create_domain(db, name=body.name, sort_order=body.sort_order)
    return DqDomainOut.model_validate(domain)


@router.patch("/domains/{domain_id}", response_model=DqDomainOut)
async def update_domain(domain_id: uuid.UUID, body: DqDomainUpdate, db: DbDep) -> DqDomainOut:
    domain = await dq_svc.get_domain(db, domain_id)
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    domain = await dq_svc.update_domain(db, domain, body.model_dump(exclude_unset=True))
    return DqDomainOut.model_validate(domain)


@router.delete("/domains/{domain_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_domain(domain_id: uuid.UUID, db: DbDep) -> None:
    domain = await dq_svc.get_domain(db, domain_id)
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    await dq_svc.delete_domain(db, domain)


# ── Catalog controls ────────────────────────────────────────────────────────

@router.get("/catalog", response_model=list[DqCatalogControlOut])
async def list_catalog_controls(
    db: DbDep,
    category: Annotated[DqCategory | None, Query()] = None,
) -> list[DqCatalogControlOut]:
    controls = await dq_svc.list_catalog_controls(db, category=category)
    return [DqCatalogControlOut.model_validate(c) for c in controls]


@router.post("/catalog", response_model=DqCatalogControlOut, status_code=status.HTTP_201_CREATED)
async def create_catalog_control(body: DqCatalogControlCreate, db: DbDep) -> DqCatalogControlOut:
    control = await dq_svc.create_catalog_control(
        db,
        category=DqCategory(body.category),
        name=body.name,
        description=body.description,
        dimension_id=body.dimension_id,
    )
    return DqCatalogControlOut.model_validate(control)


@router.get("/catalog/{control_id}", response_model=DqCatalogControlOut)
async def get_catalog_control(control_id: uuid.UUID, db: DbDep) -> DqCatalogControlOut:
    control = await dq_svc.get_catalog_control(db, control_id)
    if not control:
        raise HTTPException(status_code=404, detail="Catalog control not found")
    return DqCatalogControlOut.model_validate(control)


@router.patch("/catalog/{control_id}", response_model=DqCatalogControlOut)
async def update_catalog_control(
    control_id: uuid.UUID, body: DqCatalogControlUpdate, db: DbDep
) -> DqCatalogControlOut:
    control = await dq_svc.get_catalog_control(db, control_id)
    if not control:
        raise HTTPException(status_code=404, detail="Catalog control not found")
    fields = body.model_dump(exclude_unset=True)
    if "category" in fields and fields["category"] is not None:
        fields["category"] = DqCategory(fields["category"])
    control = await dq_svc.update_catalog_control(db, control, fields)
    return DqCatalogControlOut.model_validate(control)


@router.delete("/catalog/{control_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_catalog_control(control_id: uuid.UUID, db: DbDep) -> None:
    control = await dq_svc.get_catalog_control(db, control_id)
    if not control:
        raise HTTPException(status_code=404, detail="Catalog control not found")
    await dq_svc.delete_catalog_control(db, control)


# ── Control instances ────────────────────────────────────────────────────────

@router.get("/instances", response_model=list[DqControlInstanceOut])
async def list_control_instances(
    db: DbDep,
    domain_id: Annotated[uuid.UUID | None, Query()] = None,
    category: Annotated[DqCategory | None, Query()] = None,
) -> list[DqControlInstanceOut]:
    instances = await dq_svc.list_control_instances(db, domain_id=domain_id, category=category)
    return [DqControlInstanceOut.model_validate(i) for i in instances]


@router.post("/instances", response_model=DqControlInstanceOut, status_code=status.HTTP_201_CREATED)
async def create_control_instance(body: DqControlInstanceCreate, db: DbDep) -> DqControlInstanceOut:
    instance = await dq_svc.create_control_instance(
        db,
        domain_id=body.domain_id,
        catalog_control_id=body.catalog_control_id,
        table_ref=body.table_ref,
        field_ref=body.field_ref,
        owner=body.owner,
        risk=body.risk,
        impact=body.impact,
        status=body.status,
        notes=body.notes,
    )
    return DqControlInstanceOut.model_validate(instance)


@router.get("/instances/{instance_id}", response_model=DqControlInstanceOut)
async def get_control_instance(instance_id: uuid.UUID, db: DbDep) -> DqControlInstanceOut:
    instance = await dq_svc.get_control_instance(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Control instance not found")
    return DqControlInstanceOut.model_validate(instance)


@router.patch("/instances/{instance_id}", response_model=DqControlInstanceOut)
async def update_control_instance(
    instance_id: uuid.UUID, body: DqControlInstanceUpdate, db: DbDep
) -> DqControlInstanceOut:
    instance = await dq_svc.get_control_instance(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Control instance not found")
    instance = await dq_svc.update_control_instance(db, instance, body.model_dump(exclude_unset=True))
    return DqControlInstanceOut.model_validate(instance)


@router.delete("/instances/{instance_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_control_instance(instance_id: uuid.UUID, db: DbDep) -> None:
    instance = await dq_svc.get_control_instance(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Control instance not found")
    await dq_svc.delete_control_instance(db, instance)

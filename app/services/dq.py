from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dq import (
    DqCatalogControl,
    DqCategory,
    DqControlInstance,
    DqControlStatus,
    DqDimension,
    DqDomain,
    DqRiskLevel,
)


# ── Dimensions ────────────────────────────────────────────────────────────────

async def list_dimensions(db: AsyncSession) -> list[DqDimension]:
    result = await db.execute(select(DqDimension).order_by(DqDimension.sort_order))
    return list(result.scalars())


async def get_dimension(db: AsyncSession, dimension_id: uuid.UUID) -> DqDimension | None:
    return await db.get(DqDimension, dimension_id)


async def create_dimension(db: AsyncSession, name: str, sort_order: int = 0) -> DqDimension:
    dimension = DqDimension(name=name, sort_order=sort_order)
    db.add(dimension)
    await db.commit()
    await db.refresh(dimension)
    return dimension


async def update_dimension(db: AsyncSession, dimension: DqDimension, fields: dict) -> DqDimension:
    for k, v in fields.items():
        setattr(dimension, k, v)
    await db.commit()
    await db.refresh(dimension)
    return dimension


async def delete_dimension(db: AsyncSession, dimension: DqDimension) -> None:
    await db.delete(dimension)
    await db.commit()


# ── Domains ───────────────────────────────────────────────────────────────────

async def list_domains(db: AsyncSession) -> list[DqDomain]:
    result = await db.execute(select(DqDomain).order_by(DqDomain.sort_order))
    return list(result.scalars())


async def get_domain(db: AsyncSession, domain_id: uuid.UUID) -> DqDomain | None:
    return await db.get(DqDomain, domain_id)


# ── Catalog controls ────────────────────────────────────────────────────────

async def list_catalog_controls(
    db: AsyncSession, category: DqCategory | None = None
) -> list[DqCatalogControl]:
    q = select(DqCatalogControl).options(selectinload(DqCatalogControl.dimension))
    if category is not None:
        q = q.where(DqCatalogControl.category == category)
    q = q.order_by(DqCatalogControl.name)
    result = await db.execute(q)
    return list(result.scalars())


async def get_catalog_control(db: AsyncSession, control_id: uuid.UUID) -> DqCatalogControl | None:
    q = (
        select(DqCatalogControl)
        .options(selectinload(DqCatalogControl.dimension))
        .where(DqCatalogControl.id == control_id)
    )
    result = await db.execute(q)
    return result.scalar_one_or_none()


async def create_catalog_control(
    db: AsyncSession,
    category: DqCategory,
    name: str,
    description: str,
    dimension_id: uuid.UUID,
) -> DqCatalogControl:
    control = DqCatalogControl(
        category=category, name=name, description=description, dimension_id=dimension_id
    )
    db.add(control)
    await db.commit()
    await db.refresh(control, attribute_names=["dimension"])
    return control


async def update_catalog_control(
    db: AsyncSession, control: DqCatalogControl, fields: dict
) -> DqCatalogControl:
    for k, v in fields.items():
        setattr(control, k, v)
    await db.commit()
    await db.refresh(control, attribute_names=["dimension"])
    return control


async def delete_catalog_control(db: AsyncSession, control: DqCatalogControl) -> None:
    await db.delete(control)
    await db.commit()


# ── Control instances ────────────────────────────────────────────────────────

async def list_control_instances(
    db: AsyncSession,
    domain_id: uuid.UUID | None = None,
    category: DqCategory | None = None,
) -> list[DqControlInstance]:
    q = select(DqControlInstance).options(
        selectinload(DqControlInstance.catalog_control).selectinload(DqCatalogControl.dimension)
    )
    if domain_id is not None:
        q = q.where(DqControlInstance.domain_id == domain_id)
    if category is not None:
        q = q.join(DqCatalogControl).where(DqCatalogControl.category == category)
    q = q.order_by(DqControlInstance.table_ref, DqControlInstance.field_ref)
    result = await db.execute(q)
    return list(result.scalars())


async def get_control_instance(db: AsyncSession, instance_id: uuid.UUID) -> DqControlInstance | None:
    q = (
        select(DqControlInstance)
        .options(selectinload(DqControlInstance.catalog_control).selectinload(DqCatalogControl.dimension))
        .where(DqControlInstance.id == instance_id)
    )
    result = await db.execute(q)
    return result.scalar_one_or_none()


async def create_control_instance(
    db: AsyncSession,
    domain_id: uuid.UUID,
    catalog_control_id: uuid.UUID,
    table_ref: str,
    field_ref: str,
    owner: str | None,
    risk: DqRiskLevel,
    impact: DqRiskLevel,
    status: DqControlStatus,
    notes: str | None,
) -> DqControlInstance:
    instance = DqControlInstance(
        domain_id=domain_id,
        catalog_control_id=catalog_control_id,
        table_ref=table_ref,
        field_ref=field_ref,
        owner=owner,
        risk=risk,
        impact=impact,
        status=status,
        notes=notes,
    )
    db.add(instance)
    await db.commit()
    await db.refresh(instance, attribute_names=["catalog_control"])
    await db.refresh(instance.catalog_control, attribute_names=["dimension"])
    return instance


async def update_control_instance(
    db: AsyncSession, instance: DqControlInstance, fields: dict
) -> DqControlInstance:
    for k, v in fields.items():
        setattr(instance, k, v)
    await db.commit()
    await db.refresh(instance, attribute_names=["catalog_control"])
    await db.refresh(instance.catalog_control, attribute_names=["dimension"])
    return instance


async def delete_control_instance(db: AsyncSession, instance: DqControlInstance) -> None:
    await db.delete(instance)
    await db.commit()

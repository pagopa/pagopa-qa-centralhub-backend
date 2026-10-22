from __future__ import annotations

import uuid

import structlog
from sqlalchemy import distinct, select
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

logger = structlog.get_logger(__name__)


# ── Dimensions ────────────────────────────────────────────────────────────────

async def list_dimensions(db: AsyncSession) -> list[DqDimension]:
    try:
        result = await db.execute(select(DqDimension).order_by(DqDimension.sort_order))
        _query_result_for_logging = list(result.scalars())
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("list_dimensions query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("list_dimensions failed")
        raise


async def get_dimension(db: AsyncSession, dimension_id: uuid.UUID) -> DqDimension | None:
    try:
        _query_result_for_logging = await db.get(DqDimension, dimension_id)
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("get_dimension query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("get_dimension failed")
        raise


async def create_dimension(db: AsyncSession, name: str, sort_order: int = 0) -> DqDimension:
    try:
        dimension = DqDimension(name=name, sort_order=sort_order)
        db.add(dimension)
        await db.commit()
        await db.refresh(dimension)
        return dimension
    except Exception:
        logger.exception("create_dimension failed")
        raise


async def update_dimension(db: AsyncSession, dimension: DqDimension, fields: dict) -> DqDimension:
    try:
        for k, v in fields.items():
            setattr(dimension, k, v)
        await db.commit()
        await db.refresh(dimension)
        return dimension
    except Exception:
        logger.exception("update_dimension failed")
        raise


async def delete_dimension(db: AsyncSession, dimension: DqDimension) -> None:
    try:
        await db.delete(dimension)
        await db.commit()
    except Exception:
        logger.exception("delete_dimension failed")
        raise


# ── Domains ───────────────────────────────────────────────────────────────────

async def list_domains(db: AsyncSession) -> list[DqDomain]:
    try:
        result = await db.execute(select(DqDomain).order_by(DqDomain.sort_order))
        _query_result_for_logging = list(result.scalars())
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("list_domains query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("list_domains failed")
        raise


async def get_domain(db: AsyncSession, domain_id: uuid.UUID) -> DqDomain | None:
    try:
        _query_result_for_logging = await db.get(DqDomain, domain_id)
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("get_domain query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("get_domain failed")
        raise


async def create_domain(db: AsyncSession, name: str, sort_order: int = 0) -> DqDomain:
    try:
        domain = DqDomain(name=name, sort_order=sort_order)
        db.add(domain)
        await db.commit()
        await db.refresh(domain)
        return domain
    except Exception:
        logger.exception("create_domain failed")
        raise


async def update_domain(db: AsyncSession, domain: DqDomain, fields: dict) -> DqDomain:
    try:
        for k, v in fields.items():
            setattr(domain, k, v)
        await db.commit()
        await db.refresh(domain)
        return domain
    except Exception:
        logger.exception("update_domain failed")
        raise


async def delete_domain(db: AsyncSession, domain: DqDomain) -> None:
    try:
        await db.delete(domain)
        await db.commit()
    except Exception:
        logger.exception("delete_domain failed")
        raise


# ── Catalog controls ────────────────────────────────────────────────────────

async def list_catalog_controls(
    db: AsyncSession, category: DqCategory | None = None
) -> list[DqCatalogControl]:
    try:
        q = select(DqCatalogControl).options(selectinload(DqCatalogControl.dimension))
        if category is not None:
            q = q.where(DqCatalogControl.category == category)
        q = q.order_by(DqCatalogControl.name)
        result = await db.execute(q)
        _query_result_for_logging = list(result.scalars())
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("list_catalog_controls query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("list_catalog_controls failed")
        raise


async def get_catalog_control(db: AsyncSession, control_id: uuid.UUID) -> DqCatalogControl | None:
    try:
        q = (
            select(DqCatalogControl)
            .options(selectinload(DqCatalogControl.dimension))
            .where(DqCatalogControl.id == control_id)
        )
        result = await db.execute(q)
        _query_result_for_logging = result.scalar_one_or_none()
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("get_catalog_control query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("get_catalog_control failed")
        raise


async def create_catalog_control(
    db: AsyncSession,
    category: DqCategory,
    name: str,
    description: str,
    dimension_id: uuid.UUID,
    created_by: str | None = None,
) -> DqCatalogControl:
    try:
        control = DqCatalogControl(
            category=category,
            name=name,
            description=description,
            dimension_id=dimension_id,
            created_by=created_by,
            updated_by=created_by,
        )
        db.add(control)
        await db.commit()
        await db.refresh(control, attribute_names=["dimension"])
        return control
    except Exception:
        logger.exception("create_catalog_control failed")
        raise


async def update_catalog_control(
    db: AsyncSession, control: DqCatalogControl, fields: dict
) -> DqCatalogControl:
    try:
        for k, v in fields.items():
            setattr(control, k, v)
        await db.commit()
        await db.refresh(control)
        await db.refresh(control, attribute_names=["dimension"])
        return control
    except Exception:
        logger.exception("update_catalog_control failed")
        raise


async def delete_catalog_control(db: AsyncSession, control: DqCatalogControl) -> None:
    try:
        await db.delete(control)
        await db.commit()
    except Exception:
        logger.exception("delete_catalog_control failed")
        raise


# ── Control instances ────────────────────────────────────────────────────────

async def list_control_instances(
    db: AsyncSession,
    domain_id: uuid.UUID | None = None,
    category: DqCategory | None = None,
    status: DqControlStatus | None = None,
    table_ref: str | None = None,
) -> list[DqControlInstance]:
    try:
        q = select(DqControlInstance).options(
            selectinload(DqControlInstance.catalog_control).selectinload(DqCatalogControl.dimension)
        )
        if domain_id is not None:
            q = q.where(DqControlInstance.domain_id == domain_id)
        if category is not None:
            q = q.join(DqCatalogControl).where(DqCatalogControl.category == category)
        if status is not None:
            q = q.where(DqControlInstance.status == status)
        if table_ref is not None:
            q = q.where(DqControlInstance.table_ref == table_ref)
        q = q.order_by(DqControlInstance.table_ref, DqControlInstance.field_ref)
        result = await db.execute(q)
        _query_result_for_logging = list(result.scalars())
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("list_control_instances query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("list_control_instances failed")
        raise


async def list_control_instance_tables(
    db: AsyncSession,
    domain_id: uuid.UUID,
    category: DqCategory | None = None,
) -> list[str]:
    try:
        q = select(distinct(DqControlInstance.table_ref)).where(
            DqControlInstance.domain_id == domain_id,
            DqControlInstance.table_ref != "",
        )
        if category is not None:
            q = q.join(DqCatalogControl).where(DqCatalogControl.category == category)
        result = await db.execute(q.order_by(DqControlInstance.table_ref))
        _query_result_for_logging = list(result.scalars())
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("list_control_instance_tables query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("list_control_instance_tables failed")
        raise


async def get_control_instance(db: AsyncSession, instance_id: uuid.UUID) -> DqControlInstance | None:
    try:
        q = (
            select(DqControlInstance)
            .options(selectinload(DqControlInstance.catalog_control).selectinload(DqCatalogControl.dimension))
            .where(DqControlInstance.id == instance_id)
        )
        result = await db.execute(q)
        _query_result_for_logging = result.scalar_one_or_none()
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("get_control_instance query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("get_control_instance failed")
        raise


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
    check_name: str,
    soda_url: str | None = None,
    created_by: str | None = None,
) -> DqControlInstance:
    try:
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
            check_name=check_name,
            soda_url=soda_url,
            created_by=created_by,
            updated_by=created_by,
        )
        db.add(instance)
        await db.commit()
        await db.refresh(instance, attribute_names=["catalog_control"])
        await db.refresh(instance.catalog_control, attribute_names=["dimension"])
        return instance
    except Exception:
        logger.exception("create_control_instance failed")
        raise


async def update_control_instance(
    db: AsyncSession, instance: DqControlInstance, fields: dict
) -> DqControlInstance:
    try:
        for k, v in fields.items():
            setattr(instance, k, v)
        await db.commit()
        await db.refresh(instance)
        await db.refresh(instance, attribute_names=["catalog_control"])
        await db.refresh(instance.catalog_control, attribute_names=["dimension"])
        return instance
    except Exception:
        logger.exception("update_control_instance failed")
        raise


async def delete_control_instance(
    db: AsyncSession, instance: DqControlInstance, updated_by: str | None = None
) -> None:
    try:
        instance.status = DqControlStatus.ELIMINATO
        instance.updated_by = updated_by
        await db.commit()
    except Exception:
        logger.exception("delete_control_instance failed")
        raise

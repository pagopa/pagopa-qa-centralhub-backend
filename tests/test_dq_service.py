from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.models.dq import (
    DqCatalogControl,
    DqCategory,
    DqControlInstance,
    DqControlStatus,
    DqDimension,
    DqDomain,
    DqRiskLevel,
)
from app.services import dq as dq_svc
from tests._db import TestSession


@pytest.fixture
async def db():
    async with TestSession() as session:
        yield session
        # Clean up anything created during the test
        await session.execute(delete(DqControlInstance).where(DqControlInstance.table_ref.like("pagopa.%test%")))
        await session.execute(delete(DqCatalogControl).where(DqCatalogControl.name.like("Test %")))
        await session.execute(delete(DqDimension).where(DqDimension.name.like("Test %")))
        await session.execute(delete(DqDomain).where(DqDomain.name.like("Test %")))
        await session.commit()


@pytest.mark.anyio
async def test_list_dimensions_returns_seeded_rows(db) -> None:
    dimensions = await dq_svc.list_dimensions(db)
    names = {d.name for d in dimensions}
    assert {"Validità", "Completezza", "Consistenza", "Accuratezza", "Unicità", "Tempestività"} <= names


@pytest.mark.anyio
async def test_create_and_update_dimension(db) -> None:
    dim = await dq_svc.create_dimension(db, name="Test Dimension", sort_order=99)
    assert dim.id is not None

    updated = await dq_svc.update_dimension(db, dim, {"sort_order": 100})
    assert updated.sort_order == 100

    await dq_svc.delete_dimension(db, updated)
    assert await dq_svc.get_dimension(db, dim.id) is None


@pytest.mark.anyio
async def test_list_domains_returns_seeded_rows(db) -> None:
    domains = await dq_svc.list_domains(db)
    names = [d.name for d in domains]
    assert names == ["GEC", "GPD", "BIZ", "FDR", "Wallet"]


@pytest.mark.anyio
async def test_create_and_update_domain(db) -> None:
    domain = await dq_svc.create_domain(db, name="Test Domain", sort_order=99)
    assert domain.id is not None

    updated = await dq_svc.update_domain(db, domain, {"sort_order": 100})
    assert updated.sort_order == 100

    await dq_svc.delete_domain(db, updated)
    assert await dq_svc.get_domain(db, domain.id) is None


@pytest.mark.anyio
async def test_create_catalog_control_and_list_by_category(db) -> None:
    dimensions = await dq_svc.list_dimensions(db)
    validita = next(d for d in dimensions if d.name == "Validità")

    control = await dq_svc.create_catalog_control(
        db,
        category=DqCategory.PUNTUALE,
        name="Test Control",
        description="Test description",
        dimension_id=validita.id,
    )
    assert control.id is not None
    assert control.dimension.name == "Validità"

    items = await dq_svc.list_catalog_controls(db, category=DqCategory.PUNTUALE)
    assert any(c.id == control.id for c in items)

    updated = await dq_svc.update_catalog_control(db, control, {"description": "Updated description"})
    assert updated.description == "Updated description"
    assert updated.updated_at is not None

    await dq_svc.delete_catalog_control(db, updated)
    assert await dq_svc.get_catalog_control(db, control.id) is None


@pytest.mark.anyio
async def test_create_and_filter_control_instances(db) -> None:
    dimensions = await dq_svc.list_dimensions(db)
    validita = next(d for d in dimensions if d.name == "Validità")
    control = await dq_svc.create_catalog_control(
        db,
        category=DqCategory.PUNTUALE,
        name="Test Control For Instance",
        description="desc",
        dimension_id=validita.id,
    )

    domains = await dq_svc.list_domains(db)
    gec = next(d for d in domains if d.name == "GEC")

    instance = await dq_svc.create_control_instance(
        db,
        domain_id=gec.id,
        catalog_control_id=control.id,
        table_ref="pagopa.bronze_test_table",
        field_ref="after.test_field",
        owner=None,
        risk=DqRiskLevel.BASSO,
        impact=DqRiskLevel.ALTO,
        status=DqControlStatus.DA_IMPLEMENTARE,
        notes=None,
    )
    assert instance.id is not None
    assert instance.catalog_control.name == "Test Control For Instance"

    by_domain = await dq_svc.list_control_instances(db, domain_id=gec.id)
    assert any(i.id == instance.id for i in by_domain)

    by_other_domain = await dq_svc.list_control_instances(db, domain_id=uuid.uuid4())
    assert all(i.id != instance.id for i in by_other_domain)

    updated = await dq_svc.update_control_instance(db, instance, {"status": DqControlStatus.ATTIVO})
    assert updated.status == DqControlStatus.ATTIVO
    assert updated.updated_at is not None

    await dq_svc.delete_control_instance(db, updated)
    await dq_svc.delete_catalog_control(db, control)

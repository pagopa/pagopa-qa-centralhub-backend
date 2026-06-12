from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.models.dq import (
    DqCatalogControl,
    DqCategory,
    DqControlInstance,
    DqControlStatus,
    DqDimension,
    DqDomain,
    DqRiskLevel,
)

NOW = datetime.now(timezone.utc)


def _dimension() -> DqDimension:
    return DqDimension(id=uuid.uuid4(), name="Validità", sort_order=0)


def _domain() -> DqDomain:
    return DqDomain(id=uuid.uuid4(), name="GEC", sort_order=0, created_at=NOW, updated_at=NOW)


def _control(dimension: DqDimension) -> DqCatalogControl:
    control = DqCatalogControl(
        id=uuid.uuid4(),
        category=DqCategory.PUNTUALE,
        name="Check not null",
        description="Verifica campo required",
        dimension_id=dimension.id,
        created_at=NOW,
        updated_at=NOW,
    )
    control.dimension = dimension
    return control


def _instance(domain: DqDomain, control: DqCatalogControl) -> DqControlInstance:
    instance = DqControlInstance(
        id=uuid.uuid4(),
        domain_id=domain.id,
        catalog_control_id=control.id,
        table_ref="pagopa.bronze_gpd_payment_position",
        field_ref="after.id",
        owner=None,
        risk=DqRiskLevel.BASSO,
        impact=DqRiskLevel.ALTO,
        status=DqControlStatus.DA_IMPLEMENTARE,
        notes=None,
        created_at=NOW,
        updated_at=NOW,
    )
    instance.catalog_control = control
    return instance


@pytest.mark.anyio
async def test_list_dimensions(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.dq.dq_svc.list_dimensions", new_callable=AsyncMock, return_value=[_dimension()]
    ):
        response = await client.get("/api/v1/dq/dimensions")

    assert response.status_code == 200
    body = response.json()
    assert body[0]["name"] == "Validità"


@pytest.mark.anyio
async def test_create_dimension(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.dq.dq_svc.create_dimension",
        new_callable=AsyncMock,
        return_value=DqDimension(id=uuid.uuid4(), name="Nuova Dimensione", sort_order=6),
    ):
        response = await client.post(
            "/api/v1/dq/dimensions", json={"name": "Nuova Dimensione", "sort_order": 6}
        )

    assert response.status_code == 201
    assert response.json()["name"] == "Nuova Dimensione"


@pytest.mark.anyio
async def test_update_dimension_returns_404_when_missing(client: AsyncClient) -> None:
    with patch("app.api.v1.dq.dq_svc.get_dimension", new_callable=AsyncMock, return_value=None):
        response = await client.patch(
            f"/api/v1/dq/dimensions/{uuid.uuid4()}", json={"sort_order": 1}
        )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_list_domains(client: AsyncClient) -> None:
    with patch("app.api.v1.dq.dq_svc.list_domains", new_callable=AsyncMock, return_value=[_domain()]):
        response = await client.get("/api/v1/dq/domains")

    assert response.status_code == 200
    body = response.json()
    assert body[0]["name"] == "GEC"


@pytest.mark.anyio
async def test_list_catalog_controls_with_category_filter(client: AsyncClient) -> None:
    dimension = _dimension()
    control = _control(dimension)
    with patch(
        "app.api.v1.dq.dq_svc.list_catalog_controls", new_callable=AsyncMock, return_value=[control]
    ) as mock_list:
        response = await client.get("/api/v1/dq/catalog?category=puntuale")

    assert response.status_code == 200
    body = response.json()
    assert body[0]["name"] == "Check not null"
    assert body[0]["dimension"]["name"] == "Validità"
    mock_list.assert_awaited_once()
    assert mock_list.call_args.kwargs["category"] == DqCategory.PUNTUALE


@pytest.mark.anyio
async def test_create_catalog_control(client: AsyncClient) -> None:
    dimension = _dimension()
    control = _control(dimension)
    with patch(
        "app.api.v1.dq.dq_svc.create_catalog_control", new_callable=AsyncMock, return_value=control
    ):
        response = await client.post(
            "/api/v1/dq/catalog",
            json={
                "category": "puntuale",
                "name": "Check not null",
                "description": "Verifica campo required",
                "dimension_id": str(dimension.id),
            },
        )

    assert response.status_code == 201
    assert response.json()["category"] == "puntuale"


@pytest.mark.anyio
async def test_delete_catalog_control_returns_404_when_missing(client: AsyncClient) -> None:
    with patch("app.api.v1.dq.dq_svc.get_catalog_control", new_callable=AsyncMock, return_value=None):
        response = await client.delete(f"/api/v1/dq/catalog/{uuid.uuid4()}")

    assert response.status_code == 404


@pytest.mark.anyio
async def test_list_control_instances_with_filters(client: AsyncClient) -> None:
    dimension = _dimension()
    domain = _domain()
    control = _control(dimension)
    instance = _instance(domain, control)

    with patch(
        "app.api.v1.dq.dq_svc.list_control_instances", new_callable=AsyncMock, return_value=[instance]
    ) as mock_list:
        response = await client.get(f"/api/v1/dq/instances?domain_id={domain.id}&category=puntuale")

    assert response.status_code == 200
    body = response.json()
    assert body[0]["table_ref"] == "pagopa.bronze_gpd_payment_position"
    assert body[0]["catalog_control"]["name"] == "Check not null"
    mock_list.assert_awaited_once()
    assert mock_list.call_args.kwargs["domain_id"] == domain.id
    assert mock_list.call_args.kwargs["category"] == DqCategory.PUNTUALE


@pytest.mark.anyio
async def test_create_control_instance(client: AsyncClient) -> None:
    dimension = _dimension()
    domain = _domain()
    control = _control(dimension)
    instance = _instance(domain, control)

    with patch(
        "app.api.v1.dq.dq_svc.create_control_instance", new_callable=AsyncMock, return_value=instance
    ):
        response = await client.post(
            "/api/v1/dq/instances",
            json={
                "domain_id": str(domain.id),
                "catalog_control_id": str(control.id),
                "table_ref": "pagopa.bronze_gpd_payment_position",
                "field_ref": "after.id",
                "risk": "BASSO",
                "impact": "ALTO",
                "status": "da_implementare",
            },
        )

    assert response.status_code == 201
    assert response.json()["field_ref"] == "after.id"


@pytest.mark.anyio
async def test_update_control_instance_returns_404_when_missing(client: AsyncClient) -> None:
    with patch("app.api.v1.dq.dq_svc.get_control_instance", new_callable=AsyncMock, return_value=None):
        response = await client.patch(
            f"/api/v1/dq/instances/{uuid.uuid4()}", json={"status": "attivo"}
        )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_delete_control_instance_returns_404_when_missing(client: AsyncClient) -> None:
    with patch("app.api.v1.dq.dq_svc.get_control_instance", new_callable=AsyncMock, return_value=None):
        response = await client.delete(f"/api/v1/dq/instances/{uuid.uuid4()}")

    assert response.status_code == 404

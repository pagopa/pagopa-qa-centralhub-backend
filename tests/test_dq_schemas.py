from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.dq import DqCatalogControlCreate, DqControlInstanceCreate


def test_catalog_control_schema_has_no_functional_identifier() -> None:
    assert "catalog_code" not in DqCatalogControlCreate.model_fields


def test_active_instance_requires_check_name_and_soda_url() -> None:
    with pytest.raises(ValidationError):
        DqControlInstanceCreate(
            domain_id=uuid.uuid4(),
            catalog_control_id=uuid.uuid4(),
            table_ref="pagopa.silver_touchpoints",
            field_ref="createddate",
            risk="BASSO",
            impact="ALTO",
            status="attivo",
            check_name="fld__vld__createddate__date_format",
        )


def test_active_instance_accepts_standard_name_and_http_soda_url() -> None:
    instance = DqControlInstanceCreate(
        domain_id=uuid.uuid4(),
        catalog_control_id=uuid.uuid4(),
        table_ref="pagopa.silver_touchpoints",
        field_ref="createddate",
        risk="BASSO",
        impact="ALTO",
        status="attivo",
        check_name="fld__vld__createddate__date_format",
        soda_url="https://cloud.soda.io/checks/123",
    )

    assert instance.soda_url == "https://cloud.soda.io/checks/123"


def test_active_instance_rejects_non_http_soda_url() -> None:
    with pytest.raises(ValidationError):
        DqControlInstanceCreate(
            domain_id=uuid.uuid4(),
            catalog_control_id=uuid.uuid4(),
            table_ref="pagopa.silver_touchpoints",
            field_ref="createddate",
            risk="BASSO",
            impact="ALTO",
            status="attivo",
            check_name="fld__vld__createddate__date_format",
            soda_url="javascript:alert(1)",
        )
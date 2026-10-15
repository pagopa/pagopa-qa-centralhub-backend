from __future__ import annotations

from app.models.dq import (
    DqCatalogControl,
    DqCategory,
    DqControlInstance,
    DqControlStatus,
    DqDimension,
    DqDomain,
    DqRiskLevel,
)


def test_dq_dimension_tablename() -> None:
    assert DqDimension.__tablename__ == "dq_dimensions"


def test_dq_catalog_control_tablename() -> None:
    assert DqCatalogControl.__tablename__ == "dq_catalog_controls"


def test_dq_domain_tablename() -> None:
    assert DqDomain.__tablename__ == "dq_domains"


def test_dq_control_instance_tablename() -> None:
    assert DqControlInstance.__tablename__ == "dq_control_instances"


def test_dq_audit_columns_are_nullable() -> None:
    for model in (DqCatalogControl, DqControlInstance):
        columns = {column.name: column for column in model.__table__.columns}
        assert {"created_by", "updated_by"} <= columns.keys()
        assert columns["created_by"].nullable
        assert columns["updated_by"].nullable


def test_functional_identifiers_are_stored_only_on_control_instances() -> None:
    catalog_columns = {column.name: column for column in DqCatalogControl.__table__.columns}
    instance_columns = {column.name: column for column in DqControlInstance.__table__.columns}

    assert "catalog_code" not in catalog_columns
    assert instance_columns["check_name"].nullable
    assert instance_columns["soda_url"].nullable


def test_dq_category_enum_values() -> None:
    assert {c.value for c in DqCategory} == {"puntuale", "intra_entita", "cross_entita"}


def test_dq_risk_level_enum_values() -> None:
    assert {r.value for r in DqRiskLevel} == {"ALTO", "MEDIO", "BASSO"}


def test_dq_control_status_enum_values() -> None:
    assert {s.value for s in DqControlStatus} == {
        "da_implementare",
        "in_sviluppo",
        "attivo",
        "non_attivo",
        "eliminato",
    }


def test_dq_catalog_control_relationships() -> None:
    assert DqCatalogControl.dimension is not None
    assert DqCatalogControl.instances is not None


def test_dq_control_instance_relationships() -> None:
    assert DqControlInstance.domain is not None
    assert DqControlInstance.catalog_control is not None

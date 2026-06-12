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
    }


def test_dq_catalog_control_relationships() -> None:
    assert DqCatalogControl.dimension is not None
    assert DqCatalogControl.instances is not None


def test_dq_control_instance_relationships() -> None:
    assert DqControlInstance.domain is not None
    assert DqControlInstance.catalog_control is not None

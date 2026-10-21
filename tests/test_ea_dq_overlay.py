from __future__ import annotations

# ruff: noqa: S101
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.services.ea.dq_overlay import (
    base_name,
    check_dimension,
    coverage,
    match_controls,
    table_refs,
)
from app.services.ea.importer import import_kb
from app.services.ea.kb import build_kb, get_node_detail
from tests._db import TestSession
from tests.test_ea_data_lake_sync import ContractsGitHub, _sync
from tests.test_ea_service import _wipe
from tests.test_ea_sync import _kb

GPD = {
    "layer": "ingestion",
    "topic": "gpd-ingestion.apd.payment_position",
    "models": ["payment-position"],
    "quality": {"dataset": "pagopa.silver_gpd_payment_position"},
    "check_names": [
        "fld__cmp__id__not_null",
        "fld__vld__status__in_enum",
        "Table must contain data",
    ],
}
GEC = {"layer": "ingestion", "models": ["bundles"], "quality": {"dataset": "pagopa.silver_bundles"}}
GOLD = {"layer": "gold", "table": "pagopa.gold_payments"}
SILVER = {"layer": "silver", "table": "pagopa.silver_gpd_payment_position", "contracted": False}
DATASETS = {"gpd": GPD, "gec": GEC, "gold": GOLD, "silver": SILVER}


def ctl(
    table_ref: str, check: str | None = None, status: str = "attivo", dim: str = "Completezza"
) -> dict[str, Any]:
    return {
        "table_ref": table_ref,
        "check_name": check,
        "status": status,
        "dimension": dim,
        "domain": "GPD",
    }


def test_helpers() -> None:
    assert base_name("pagopa.bronze_gpd_transfer") == "gpd_transfer"
    assert base_name("silver_bundles") == "bundles"
    assert table_refs("pagopa.bronze_a pagopa.bronze_b\npagopa.bronze_c") == [
        "pagopa.bronze_a",
        "pagopa.bronze_b",
        "pagopa.bronze_c",
    ]
    assert check_dimension("fld__cmp__id__not_null") == "Completezza"
    assert check_dimension("ent__unq__dataset__pk") == "Unicità"
    assert check_dimension("Table must contain data") == "Altro"


def test_bronze_controls_go_to_the_ingestion_and_tables_to_their_dataset() -> None:
    matched = match_controls(
        DATASETS,
        [
            ctl("pagopa.bronze_gpd_payment_position", "fld__cmp__id__not_null"),
            ctl("pagopa.bronze_bundles"),
            ctl("pagopa.gold_payments"),
            ctl("pagopa.silver_gpd_payment_position"),  # exact table wins over the ingestion
            # cross-entity: one control, two datasets
            ctl("pagopa.bronze_gpd_payment_position pagopa.bronze_bundles"),
            ctl("pagopa.bronze_unknown"),
        ],
    )
    assert {k: len(v) for k, v in matched.items()} == {"gpd": 2, "gec": 2, "gold": 1, "silver": 1}


def test_coverage_summary_and_alignment_of_check_names() -> None:
    controls = [
        ctl("x", "fld__cmp__id__not_null"),
        ctl("x", "fld__cmp__iupd__not_null", status="in_sviluppo", dim="Completezza"),
        ctl("x", None, dim="Validità"),
    ]
    cov = coverage(GPD, controls)
    assert cov["contract_checks"] == 3
    assert cov["contract_dimensions"] == {"Completezza": 1, "Validità": 1, "Altro": 1}
    assert (cov["controls"], cov["active"]) == (3, 2)
    assert cov["statuses"] == {"attivo": 2, "in_sviluppo": 1}
    assert cov["dimensions"] == {"Completezza": 2, "Validità": 1}
    assert cov["aligned"] == 1
    assert cov["only_contract"] == ["Table must contain data", "fld__vld__status__in_enum"]
    assert cov["only_hub"] == ["fld__cmp__iupd__not_null"]


CONTROL_NAME = "EA overlay test – not null"


async def _wipe_dq(session: AsyncSession) -> None:
    """Controls start empty; dimensions and domains are seeded data and stay."""
    await session.execute(delete(DqControlInstance))
    await session.execute(delete(DqCatalogControl))
    await session.commit()


async def _get_or_create(
    session: AsyncSession, model: type[DqDimension] | type[DqDomain], name: str
) -> Any:
    row = await session.scalar(select(model).where(model.name == name))
    if row is None:
        row = model(name=name, sort_order=99)
        session.add(row)
        await session.commit()
    return row


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        await _wipe(session)
        await _wipe_dq(session)
        kb = _kb()
        kb["nodes"].append(
            {"id": "ext-bdi", "type": "external", "name": "Banca d'Italia", "desc": ""}
        )
        await import_kb(session, kb)
        yield session
        await session.rollback()
        await _wipe(session)
        await _wipe_dq(session)


async def test_kb_and_detail_show_the_dq_coverage(db: AsyncSession) -> None:
    await _sync(db, ContractsGitHub())
    dim = await _get_or_create(db, DqDimension, "Completezza")
    domain = await _get_or_create(db, DqDomain, "GEC")
    control = await dq_svc.create_catalog_control(
        db, DqCategory.PUNTUALE, CONTROL_NAME, "Il campo è valorizzato", dim.id
    )
    for field, check, status in (
        ("id", "fld__cmp__id__not_null", DqControlStatus.ATTIVO),
        ("idPsp", "fld__cmp__idpsp__not_null", DqControlStatus.IN_SVILUPPO),
        ("x", "fld__cmp__x__not_null", DqControlStatus.ELIMINATO),  # not counted
    ):
        await dq_svc.create_control_instance(
            db,
            domain_id=domain.id,
            catalog_control_id=control.id,
            table_ref="pagopa.bronze_bundles",
            field_ref=field,
            owner=None,
            risk=DqRiskLevel.ALTO,
            impact=DqRiskLevel.MEDIO,
            status=status,
            notes=None,
            check_name=check,
            soda_url="https://soda.example.test/x" if status == DqControlStatus.ATTIVO else None,
        )

    kb = await build_kb(db)
    bundles = next(n for n in kb["nodes"] if n["id"] == "dc:pagopa:gec:bundles")
    assert (bundles["dq"], bundles["dqActive"]) == (2, 1)
    gold = next(n for n in kb["nodes"] if n["id"] == "dc:pagopa:gold:payments")
    assert gold["dq"] == 0

    detail = await get_node_detail(db, "dc:pagopa:gec:bundles")
    assert detail is not None and detail["dq"] is not None
    dq = detail["dq"]
    assert dq["controls"] == 2 and dq["domains"] == ["GEC"]
    assert dq["contract_checks"] == 3 and dq["aligned"] == 2  # id and idpsp are in both
    assert dq["only_contract"] == ["ent__cmp__dataset__is_not_empty"]
    assert {i["check_name"] for i in dq["items"]} == {
        "fld__cmp__id__not_null",
        "fld__cmp__idpsp__not_null",
    }
    assert (await get_node_detail(db, "pagopa-wisp-converter") or {})["dq"] is None

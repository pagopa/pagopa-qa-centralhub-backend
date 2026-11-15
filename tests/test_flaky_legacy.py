from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.flaky import FlakyRegistry
from app.models.test_metrics import FlakyScenario
from app.models.test_metrics import TestSuite as SuiteModel
from app.schemas.test_metrics import FlakyScenarioIn
from app.services.test_metrics import get_flaky_scenarios, save_or_update_flaky_scenarios
from tests._db import TestSession


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def suites(db: AsyncSession) -> AsyncIterator[tuple[SuiteModel, SuiteModel]]:
    test_object = f"legacy-{uuid.uuid4().hex[:8]}"
    v1 = SuiteModel(test_object=test_object, test_type="e2e", test_object_version="1.0.0")
    v2 = SuiteModel(test_object=test_object, test_type="e2e", test_object_version="2.0.0")
    db.add_all([v1, v2])
    await db.commit()
    yield v1, v2
    await db.rollback()
    registry_ids = await db.scalars(
        select(FlakyRegistry.id).where(FlakyRegistry.test_object == test_object)
    )
    audit_ids = [str(registry_id) for registry_id in registry_ids.all()]
    await db.execute(delete(AuditLog).where(AuditLog.resource_id.in_(audit_ids)))
    await db.execute(delete(FlakyRegistry).where(FlakyRegistry.test_object == test_object))
    await db.execute(delete(SuiteModel).where(SuiteModel.test_object == test_object))
    await db.commit()


def _item(
    suite: SuiteModel, test_version: str, is_flaky: bool, user: str | None = "me@x.it"
) -> FlakyScenarioIn:
    return FlakyScenarioIn(
        suite_id=suite.id,
        test_object=suite.test_object,
        scenario_name="Pay",
        test_object_version=suite.test_object_version,
        test_version=test_version,
        is_flaky=is_flaky,
        user=user,
    )


async def test_save_writes_registry_not_legacy_table(
    db: AsyncSession, suites: tuple[SuiteModel, SuiteModel]
) -> None:
    v1, _ = suites
    saved = await save_or_update_flaky_scenarios(db, [_item(v1, "1", True)])

    assert saved is not None
    assert [(s.test_type, s.scenario_name, s.is_flaky) for s in saved] == [("e2e", "Pay", True)]
    legacy = await db.scalar(
        select(func.count()).select_from(FlakyScenario).where(FlakyScenario.suite_id == v1.id)
    )
    assert legacy == 0
    registry = await db.scalar(
        select(FlakyRegistry).where(FlakyRegistry.test_object == v1.test_object)
    )
    assert registry is not None
    assert (registry.flagged_by, registry.updated_by) == ("me@x.it", "me@x.it")


async def test_unflag_from_old_version_unflags_scenario(
    db: AsyncSession, suites: tuple[SuiteModel, SuiteModel]
) -> None:
    v1, v2 = suites
    await save_or_update_flaky_scenarios(db, [_item(v2, "2", True)])
    await save_or_update_flaky_scenarios(db, [_item(v1, "1", False)])

    rows = (
        await db.scalars(select(FlakyRegistry).where(FlakyRegistry.test_object == v1.test_object))
    ).all()
    assert [(r.is_flaky, r.test_type) for r in rows] == [(False, "e2e")]


async def test_save_accepts_registry_items_without_version(
    db: AsyncSession, suites: tuple[SuiteModel, SuiteModel]
) -> None:
    v1, _ = suites
    item = FlakyScenarioIn(
        test_object=v1.test_object, test_type="e2e", scenario_name="Pay", is_flaky=True
    )
    saved = await save_or_update_flaky_scenarios(db, [item])

    assert saved is not None and saved[0].test_type == "e2e"
    registry = await db.scalar(
        select(FlakyRegistry).where(FlakyRegistry.test_object == v1.test_object)
    )
    assert registry is not None and registry.updated_by is None


def test_item_needs_a_test_type_or_a_suite() -> None:
    with pytest.raises(ValueError, match="test_type or suite_id"):
        FlakyScenarioIn(test_object="x", scenario_name="Pay", is_flaky=True)


async def test_get_returns_flagged_registry_rows_for_test_object(
    db: AsyncSession, suites: tuple[SuiteModel, SuiteModel]
) -> None:
    v1, _ = suites
    await save_or_update_flaky_scenarios(db, [_item(v1, "1", True)])
    other = FlakyScenarioIn(
        test_object=v1.test_object, test_type="api", scenario_name="Off", is_flaky=False
    )
    await save_or_update_flaky_scenarios(db, [other])

    rows = await get_flaky_scenarios(db, v1.test_object)

    assert rows is not None
    assert [
        (r.test_type, r.scenario_name, r.is_flaky, r.suite_id, r.test_version) for r in rows
    ] == [
        ("e2e", "Pay", True, None, None),
    ]


@pytest.mark.anyio
async def test_get_endpoint_accepts_a_test_object_name(
    client: AsyncClient, suites: tuple[SuiteModel, SuiteModel], db: AsyncSession
) -> None:
    v1, _ = suites
    await save_or_update_flaky_scenarios(db, [_item(v1, "1", True)])

    response = await client.get(
        "/api/v1/test-metrics/flaky-scenarios", params={"test_object": v1.test_object}
    )

    assert response.status_code == 200
    assert response.json()[0]["test_type"] == "e2e"


async def test_resaving_an_unchanged_flag_keeps_who_flagged_it(
    db: AsyncSession, suites: tuple[SuiteModel, SuiteModel]
) -> None:
    v1, v2 = suites
    await save_or_update_flaky_scenarios(db, [_item(v1, "1", True, user="first@x.it")])
    # The report panels re-post every known flag when saving a run.
    await save_or_update_flaky_scenarios(db, [_item(v2, "2", True, user="second@x.it")])

    registry = await db.scalar(
        select(FlakyRegistry).where(FlakyRegistry.test_object == v1.test_object)
    )
    assert registry is not None
    assert (registry.flagged_by, registry.updated_by) == ("first@x.it", "first@x.it")
    audits = await db.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.resource_id == str(registry.id))
    )
    assert audits == 1

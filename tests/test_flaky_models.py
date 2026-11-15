from __future__ import annotations

# ruff: noqa: S101
import importlib.util
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint, delete, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.flaky import FlakyRecomputeStatus, FlakyRegistry, FlakyScore
from app.models.test_metrics import FlakyScenario
from app.models.test_metrics import TestSuite as SuiteModel
from tests._db import TestSession

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0031_flaky_registry_and_scores.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0031", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_chains_after_0030() -> None:
    module = _migration()
    assert (module.revision, module.down_revision) == ("0031", "0030")


def test_flaky_tables_are_in_qachub_schema() -> None:
    for model, name in [
        (FlakyRegistry, "flaky_registry"),
        (FlakyScore, "flaky_scores"),
        (FlakyRecomputeStatus, "flaky_recompute_status"),
    ]:
        assert model.__tablename__ == name
        assert model.__table__.schema == "qachub"


def test_registry_key_is_unique_and_version_independent() -> None:
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in FlakyRegistry.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("test_object", "test_type", "scenario_name") in unique_columns
    assert "suite_id" not in FlakyRegistry.__table__.c
    assert "test_version" not in FlakyRegistry.__table__.c


def test_scores_are_keyed_per_scenario_and_environment() -> None:
    assert [c.name for c in FlakyScore.__table__.primary_key.columns] == [
        "test_object",
        "test_type",
        "scenario_name",
        "env",
    ]
    assert isinstance(FlakyScore.__table__.c.recent_outcomes.type, JSONB)
    suite_fk = next(iter(FlakyScore.__table__.c.last_suite_id.foreign_keys))
    assert suite_fk.ondelete == "SET NULL"


def test_recompute_status_is_a_singleton() -> None:
    checks = [
        c for c in FlakyRecomputeStatus.__table__.constraints if isinstance(c, CheckConstraint)
    ]
    assert any("id = 1" in str(check.sqltext) for check in checks)


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def legacy_suites(db: AsyncSession) -> AsyncIterator[tuple[str, list[SuiteModel]]]:
    test_object = f"legacy-{uuid.uuid4().hex[:8]}"
    suites = [
        SuiteModel(test_object=test_object, test_type="e2e", test_object_version="1.0.0"),
        SuiteModel(test_object=test_object, test_type="e2e", test_object_version="2.0.0"),
        SuiteModel(test_object=test_object, test_type="api", test_object_version="1.0.0"),
    ]
    db.add_all(suites)
    await db.commit()
    yield test_object, suites
    await db.rollback()
    await db.execute(delete(FlakyRegistry).where(FlakyRegistry.test_object == test_object))
    await db.execute(delete(SuiteModel).where(SuiteModel.test_object == test_object))
    await db.commit()


async def test_legacy_flags_collapse_to_latest_per_scenario(
    db: AsyncSession, legacy_suites: tuple[str, list[SuiteModel]]
) -> None:
    test_object, (v1, v2, api) = legacy_suites
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    t1 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    db.add_all(
        [
            FlakyScenario(
                suite_id=v1.id,
                test_object=test_object,
                scenario_name="S",
                test_object_version="1.0.0",
                test_version="1",
                inserted_at=t0,
                is_flaky=True,
            ),
            FlakyScenario(
                suite_id=v2.id,
                test_object=test_object,
                scenario_name="S",
                test_object_version="2.0.0",
                test_version="2",
                inserted_at=t1,
                is_flaky=False,
            ),
            FlakyScenario(
                suite_id=api.id,
                test_object=test_object,
                scenario_name="S",
                test_object_version="1.0.0",
                test_version="1",
                inserted_at=t0,
                is_flaky=True,
            ),
        ]
    )
    await db.commit()

    await db.execute(text(_migration().COPY_LEGACY_FLAGS_SQL))
    await db.commit()

    rows = (
        (
            await db.execute(
                select(FlakyRegistry)
                .where(FlakyRegistry.test_object == test_object)
                .order_by(FlakyRegistry.test_type)
            )
        )
        .scalars()
        .all()
    )
    assert [(r.test_type, r.scenario_name, r.is_flaky, r.updated_at) for r in rows] == [
        ("api", "S", True, t0),
        ("e2e", "S", False, t1),
    ]
    api_row, e2e_row = rows
    assert (api_row.flagged_at, api_row.flagged_by, api_row.updated_by) == (t0, None, None)
    assert e2e_row.flagged_at is None

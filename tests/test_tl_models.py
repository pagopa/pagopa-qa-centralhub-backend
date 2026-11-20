from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureStatus,
    TlPolarity,
    TlRevisionStatus,
    TlScenario,
    TlScenarioContent,
    TlScenarioKind,
    TlScope,
)
from tests._db import TestSession


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    prefix = f"T{uuid.uuid4().hex[:8]}"
    async with TestSession() as session:
        session.info["prefix"] = prefix
        yield session
        await session.rollback()
    # Scopes cascade (FK ON DELETE CASCADE) to features, revisions, scenarios and contents.
    async with TestSession() as session:
        await session.execute(delete(TlScope).where(TlScope.code.like(f"{prefix}%")))
        await session.commit()


async def _scope(db: AsyncSession, suffix: str = "a") -> TlScope:
    scope = TlScope(code=f"{db.info['prefix']}{suffix}", name="Test scope")
    db.add(scope)
    await db.flush()
    return scope


def _feature(scope: TlScope, number: int) -> TlFeature:
    return TlFeature(
        scope_id=scope.id,
        number=number,
        code=f"FEAT_{number:03d}_{scope.code}",
        status=TlFeatureStatus.DRAFT,
        created_by="t@example.com",
    )


def _revision(feature: TlFeature, number: int = 1) -> TlFeatureRevision:
    return TlFeatureRevision(
        feature_id=feature.id,
        number=number,
        status=TlRevisionStatus.DRAFT,
        title="Titolo",
        created_by="t@example.com",
    )


async def test_feature_defaults_and_revision_link(db: AsyncSession) -> None:
    scope = await _scope(db)
    feature = _feature(scope, 1)
    db.add(feature)
    await db.flush()
    rev = _revision(feature)
    db.add(rev)
    await db.flush()
    feature.working_revision_id = rev.id
    await db.flush()
    assert scope.next_feature_number == 1
    assert feature.next_scenario_number == 1
    assert rev.lock_version == 0
    assert rev.reverse_engineering is False


async def test_feature_number_unique_per_scope(db: AsyncSession) -> None:
    scope = await _scope(db)
    db.add(_feature(scope, 1))
    await db.flush()
    dup = _feature(scope, 1)
    dup.code = f"OTHER_{scope.code}"
    db.add(dup)
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_scenario_number_unique_per_feature(db: AsyncSession) -> None:
    scope = await _scope(db)
    feature = _feature(scope, 1)
    db.add(feature)
    await db.flush()
    db.add(TlScenario(feature_id=feature.id, number=1, tag="@X_001_01"))
    await db.flush()
    db.add(TlScenario(feature_id=feature.id, number=1, tag="@X_001_01b"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_scenario_content_unique_per_revision(db: AsyncSession) -> None:
    scope = await _scope(db)
    feature = _feature(scope, 1)
    db.add(feature)
    await db.flush()
    rev = _revision(feature)
    scenario = TlScenario(feature_id=feature.id, number=1, tag="@X_001_01")
    db.add_all([rev, scenario])
    await db.flush()

    def content() -> TlScenarioContent:
        return TlScenarioContent(
            revision_id=rev.id,
            scenario_id=scenario.id,
            position=1,
            title="S",
            kind=TlScenarioKind.SCENARIO,
            polarity=TlPolarity.POSITIVO,
            steps="Dato x",
        )

    db.add(content())
    await db.flush()
    db.add(content())
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_scope_code_unique_case_insensitive(db: AsyncSession) -> None:
    code = f"{db.info['prefix']}FdR3"
    db.add(TlScope(code=code, name="a"))
    await db.flush()
    db.add(TlScope(code=code.lower(), name="b"))
    with pytest.raises(IntegrityError):
        await db.flush()

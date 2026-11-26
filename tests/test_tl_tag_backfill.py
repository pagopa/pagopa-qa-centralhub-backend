# ruff: noqa: S101
"""Feature test type column and the 0044 backfills (design 2026-11-26, tag catalog)."""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureStatus,
    TlPolarity,
    TlRevisionStatus,
    TlScenario,
    TlScenarioContent,
    TlScenarioKind,
    TlScope,
)
from tests._db import TestSession
from tests.test_tl_features import _use_test_db  # noqa: F401  (autouse fixture)
from tests.test_tl_jira_history import _feature_with_revisions


def _migration():
    path = (
        Path(__file__).parent.parent
        / "alembic"
        / "versions"
        / "0044_feature_test_type_and_status_tags.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0044", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _scope_feature(s, repo_folder, **feature_fields):
    scope = TlScope(
        code="B" + uuid.uuid4().hex[:8], name="Backfill", repo_folder=repo_folder
    )
    s.add(scope)
    await s.flush()
    feature = TlFeature(
        scope_id=scope.id,
        number=1,
        code=f"FEAT_001_{scope.code}",
        status=TlFeatureStatus.DRAFT,
        created_by="t@example.test",
        **feature_fields,
    )
    s.add(feature)
    await s.flush()
    return scope, feature


@pytest.mark.asyncio
async def test_feature_has_test_type_column():
    async with TestSession() as s:
        scope, feature = await _scope_feature(s, "src/api", test_type="api")
        await s.commit()
        try:
            async with TestSession() as other:  # fresh session: proves the column is persisted
                assert (await other.get(TlFeature, feature.id)).test_type == "api"
        finally:
            await s.execute(delete(TlScope).where(TlScope.id == scope.id))
            await s.commit()


@pytest.mark.asyncio
async def test_test_type_rejects_unknown_values():
    async with TestSession() as s:
        scope, feature = await _scope_feature(s, "src/api")
        scope_id, feature_id = scope.id, feature.id  # read before the commit expires them
        await s.commit()
        try:
            with pytest.raises(IntegrityError):
                await s.execute(
                    text("UPDATE qachub.tl_features SET test_type = 'smoke' WHERE id = :i"),
                    {"i": feature_id},
                )
            await s.rollback()
        finally:
            await s.execute(delete(TlScope).where(TlScope.id == scope_id))
            await s.commit()


@pytest.mark.asyncio
async def test_backfill_test_type_from_scope_folder():
    sql = _migration().BACKFILL_TEST_TYPE_SQL
    async with TestSession() as s:
        made = {}
        for key, folder in {
            "integration": "src/integration/fdr",
            "e2e": "/src/e2e/",
            "api": "src/api",
            "none": "docs",
            "nofolder": None,
        }.items():
            made[key] = await _scope_feature(s, folder)
        preset_scope, preset = await _scope_feature(s, "src/integration/x", test_type="api")
        await s.commit()
        try:
            await s.execute(text(sql))
            await s.execute(text(sql))  # idempotent
            await s.commit()
            async with TestSession() as other:
                got = {
                    k: (await other.get(TlFeature, f.id)).test_type for k, (_, f) in made.items()
                }
                assert got == {
                    "integration": "integration",
                    "e2e": "e2e",
                    "api": "api",
                    "none": None,
                    "nofolder": None,
                }
                assert (await other.get(TlFeature, preset.id)).test_type == "api"
        finally:
            ids = [sc.id for sc, _ in made.values()] + [preset_scope.id]
            await s.execute(delete(TlScope).where(TlScope.id.in_(ids)))
            await s.commit()


CASES = {
    "both": (["runnable", "skip"], ["skip"]),
    "none": ([], ["runnable"]),
    "polarity": (["smoke", "positive"], ["smoke", "runnable"]),
    "legacy_polarity": (["positivo", "smoke"], ["smoke", "runnable"]),
    "broken": (["broken"], ["broken"]),
    "clean": (["smoke", "runnable", "flaky"], ["smoke", "runnable", "flaky"]),
}


async def _seed_contents(s, feature, rev, cases):
    ids = {}
    for number, (key, (given, _)) in enumerate(cases.items(), start=1):
        scenario = TlScenario(
            feature_id=feature.id,
            number=number + 100 * rev.number,
            tag=f"@T_{rev.number}{number:02d}",
        )
        s.add(scenario)
        await s.flush()
        content = TlScenarioContent(
            revision_id=rev.id,
            scenario_id=scenario.id,
            position=number,
            title=key,
            kind=TlScenarioKind.SCENARIO,
            polarity=TlPolarity.POSITIVO,
            extra_tags=given,
            steps="Dato che x",
        )
        s.add(content)
        await s.flush()
        ids[key] = content.id
    return ids


async def _tags_of(ids):
    async with TestSession() as other:
        rows = {
            r.id: r.extra_tags
            for r in (
                await other.execute(
                    select(TlScenarioContent).where(TlScenarioContent.id.in_(list(ids.values())))
                )
            ).scalars()
        }
    return {key: rows[cid] for key, cid in ids.items()}


@pytest.mark.asyncio
async def test_backfill_status_tags_on_a_draft_leaves_exactly_one_and_is_idempotent():
    module = _migration()
    async with TestSession() as s:
        scope, feature, (rev,) = await _feature_with_revisions(s)
        rev.status = TlRevisionStatus.DRAFT
        ids = await _seed_contents(s, feature, rev, CASES)
        await s.commit()
        try:
            await s.run_sync(lambda session: module.backfill_status_tags(session))
            await s.run_sync(lambda session: module.backfill_status_tags(session))
            await s.commit()
            got = await _tags_of(ids)
            assert got == {key: expected for key, (_, expected) in CASES.items()}
        finally:
            await s.execute(delete(TlScope).where(TlScope.id == scope.id))
            await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        TlRevisionStatus.MERGED,
        TlRevisionStatus.SUPERSEDED,
        TlRevisionStatus.APPROVED,
        TlRevisionStatus.IN_REVIEW,
    ],
)
async def test_backfill_leaves_published_and_historical_revisions_alone(status):
    # their content mirrors the files already in the repo: rewriting it would make the next
    # Git import see a difference and open a new revision
    module = _migration()
    async with TestSession() as s:
        scope, feature, (rev,) = await _feature_with_revisions(s)
        rev.status = status
        ids = await _seed_contents(s, feature, rev, CASES)
        await s.commit()
        try:
            await s.run_sync(lambda session: module.backfill_status_tags(session))
            await s.commit()
            got = await _tags_of(ids)
            assert got == {key: given for key, (given, _) in CASES.items()}
        finally:
            await s.execute(delete(TlScope).where(TlScope.id == scope.id))
            await s.commit()

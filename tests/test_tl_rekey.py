# ruff: noqa: F811, S101
"""Adapting a feature to another scope: code, number, scenario tags, aliases (design 2026-11-27)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, update

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureStatus,
    TlMigrationItem,
    TlRevisionStatus,
    TlScenario,
    TlScenarioAlias,
    TlScope,
)
from app.services.test_lifecycle import rekey
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _new_scenario,
    _use_test_db,
    actor,
    scope,
)


@pytest.fixture
async def target():
    code = "W" + uuid.uuid4().hex[:8]
    async with TestSession() as s:
        sc = TlScope(code=code, name="Destinazione", repo_folder="src/integration/wisp")
        s.add(sc)
        await s.commit()
        scope_id = sc.id
    yield {"id": scope_id, "code": code}
    async with TestSession() as s:
        await s.execute(delete(TlScope).where(TlScope.id == scope_id))
        await s.commit()


async def _feature(client, scope, actor, scenarios=2):
    feat = await _new_feature(client, scope, actor)
    for i in range(scenarios):
        await _new_scenario(client, feat, actor, title=f"S{i}")
    return uuid.UUID(feat["id"]), uuid.UUID(feat["working_revision"]["id"])


async def _adapt(feature_id, target, number):
    async with TestSession() as s:
        feature = await s.get(TlFeature, feature_id)
        scope = await s.get(TlScope, target["id"])
        out = await rekey.adapt_feature(s, feature, scope, number)
        await s.commit()
        return out


async def _tags(feature_id):
    async with TestSession() as s:
        return list(
            (
                await s.execute(
                    select(TlScenario.tag)
                    .where(TlScenario.feature_id == feature_id)
                    .order_by(TlScenario.number)
                )
            ).scalars()
        )


async def test_adapt_changes_code_number_scope_and_every_scenario_tag(client, scope, actor, target):
    fid, _ = await _feature(client, scope, actor)
    old_tags = await _tags(fid)
    old_code, new_code = await _adapt(fid, target, 7)
    assert new_code == f"FEAT_007_{target['code']}" and old_code != new_code
    async with TestSession() as s:
        feature = await s.get(TlFeature, fid)
        assert (feature.scope_id, feature.number, feature.code) == (target["id"], 7, new_code)
    prefix = target["code"].upper()
    assert await _tags(fid) == [f"@{prefix}_007_01", f"@{prefix}_007_02"]
    async with TestSession() as s:
        aliases = set((await s.execute(select(TlScenarioAlias.alias))).scalars())
    assert {t.lstrip("@") for t in old_tags} <= aliases  # the old tags still identify the scenarios


async def test_an_existing_alias_is_not_duplicated(client, scope, actor, target):
    fid, _ = await _feature(client, scope, actor, scenarios=1)
    (old_tag,) = await _tags(fid)
    async with TestSession() as s:
        sid = (
            await s.execute(select(TlScenario.id).where(TlScenario.feature_id == fid))
        ).scalar_one()
        s.add(TlScenarioAlias(scenario_id=sid, alias=old_tag.lstrip("@")))
        await s.commit()
    await _adapt(fid, target, 3)
    async with TestSession() as s:
        n = len(
            list(
                (
                    await s.execute(
                        select(TlScenarioAlias).where(TlScenarioAlias.alias == old_tag.lstrip("@"))
                    )
                ).scalars()
            )
        )
    assert n == 1


async def test_a_default_working_branch_follows_the_code_a_custom_one_does_not(
    client, scope, actor, target
):
    fid, rid = await _feature(client, scope, actor)
    async with TestSession() as s:
        old_code = (await s.get(TlFeature, fid)).code
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == rid)
            .values(work_branch=f"qa-hub/{old_code}-r1")
        )
        await s.commit()
    _, new_code = await _adapt(fid, target, 4)
    async with TestSession() as s:
        assert (await s.get(TlFeatureRevision, rid)).work_branch == f"qa-hub/{new_code}-r1"
    fid2, rid2 = await _feature(client, scope, actor)
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == rid2)
            .values(work_branch="feature/mia")
        )
        await s.commit()
    await _adapt(fid2, target, 5)
    async with TestSession() as s:
        assert (await s.get(TlFeatureRevision, rid2)).work_branch == "feature/mia"


async def test_the_migration_item_follows(client, scope, actor, target):
    fid, _ = await _feature(client, scope, actor)
    async with TestSession() as s:
        s.add(
            TlMigrationItem(
                feature_id=fid, scope_code=scope["code"], number=1, title="T",
                status=TlFeatureStatus.DRAFT, overrides={"scope_code": scope["code"]},
            )
        )
        await s.commit()
    await _adapt(fid, target, 9)
    async with TestSession() as s:
        item = (
            await s.execute(select(TlMigrationItem).where(TlMigrationItem.feature_id == fid))
        ).scalar_one()
        assert (item.scope_code, item.number) == (target["code"], 9)
        assert item.overrides["scope_code"] == target["code"]
        await s.delete(item)
        await s.commit()


# ── who can be adapted ───────────────────────────────────────────────────────────


async def _blockers(fid):
    async with TestSession() as s:
        return await rekey.adapt_blockers(s, await s.get(TlFeature, fid))


async def _publish_in_db(fid, rid, *, alignment_pending):
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision).where(TlFeatureRevision.id == rid)
            .values(status=TlRevisionStatus.MERGED)
        )
        await s.execute(
            update(TlFeature).where(TlFeature.id == fid).values(
                published_revision_id=rid, working_revision_id=None,
                alignment_pending=alignment_pending, git_path="src/x/a.feature",
                status=TlFeatureStatus.IMPLEMENTED,
            )
        )
        await s.commit()


async def test_a_plain_draft_can_be_adapted(client, scope, actor):
    fid, _ = await _feature(client, scope, actor)
    assert await _blockers(fid) == []


async def test_a_migrated_published_feature_waiting_for_alignment_can_be_adapted(
    client, scope, actor
):
    fid, rid = await _feature(client, scope, actor)
    await _publish_in_db(fid, rid, alignment_pending=True)
    assert await _blockers(fid) == []


async def test_a_feature_on_main_with_hub_identifiers_is_blocked(client, scope, actor):
    fid, rid = await _feature(client, scope, actor)
    await _publish_in_db(fid, rid, alignment_pending=False)
    reasons = await _blockers(fid)
    assert len(reasons) == 1 and "main" in reasons[0]


async def test_a_feature_with_an_open_pr_is_blocked(client, scope, actor):
    fid, rid = await _feature(client, scope, actor)
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision).where(TlFeatureRevision.id == rid).values(pr_state="open")
        )
        await s.commit()
    reasons = await _blockers(fid)
    assert len(reasons) == 1 and "PR" in reasons[0]


async def test_a_draft_that_pushed_its_branch_is_blocked(client, scope, actor):
    fid, rid = await _feature(client, scope, actor)
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision).where(TlFeatureRevision.id == rid)
            .values(work_branch="qa-hub/x-r1", work_hub_sha="a" * 40)
        )
        await s.commit()
    reasons = await _blockers(fid)
    assert len(reasons) == 1 and "branch" in reasons[0].lower()


async def test_adapting_a_blocked_feature_raises_and_changes_nothing(client, scope, actor, target):
    fid, rid = await _feature(client, scope, actor)
    await _publish_in_db(fid, rid, alignment_pending=False)
    before = await _tags(fid)
    with pytest.raises(rekey.AdaptBlockedError) as exc:
        await _adapt(fid, target, 2)
    assert exc.value.blocked[0]["feature_id"] == fid and exc.value.blocked[0]["reasons"]
    assert await _tags(fid) == before
    async with TestSession() as s:
        assert (await s.get(TlFeature, fid)).scope_id != target["id"]

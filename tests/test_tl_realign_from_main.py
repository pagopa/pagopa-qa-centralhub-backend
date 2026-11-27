# ruff: noqa: F811, S101
"""Recovery: a feature emptied by a legacy file on main is rebuilt from that file (main wins)."""
from __future__ import annotations

from sqlalchemy import func, select

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureStatus,
    TlIssue,
    TlIssueKind,
    TlIssueStatus,
    TlRevisionStatus,
    TlScenario,
    TlScenarioContent,
    TlScope,
)
from app.services.test_lifecycle import git_import_content as content
from app.services.test_lifecycle import migration_align, realign
from tests._db import TestSession
from tests.test_tl_features import _use_test_db, actor  # noqa: F401
from tests.test_tl_migration_execute import LEGACY, _analyze_confirm, _execute, env  # noqa: F401
from tests.tl_migration_fixtures import clean_migration  # noqa: F401

CHANGED = LEGACY.replace("Quando pago\n", "Quando pago con carta\n").replace(
    "Funzionalità: Pagamento", "Funzionalità: Pagamento aggiornato"
)


async def _migrated(env):
    s, conf, git, _ = env
    await _analyze_confirm(conf, git, s)
    out = await _execute(env)
    git.merge_pull(out.pr_number)
    async with TestSession() as db:
        await migration_align.sync_alignment_prs(db, git)
    async with TestSession() as db:
        scope = (await db.execute(select(TlScope).where(TlScope.code == s))).scalar_one()
        return (
            await db.execute(
                select(TlFeature).where(TlFeature.scope_id == scope.id, TlFeature.number == 1)
            )
        ).scalar_one().id


async def _wipe(feature_id, sha, with_draft=False):
    """The incident: a git revision without scenarios replaces the published one."""
    async with TestSession() as db:
        f = await db.get(TlFeature, feature_id)
        old = await db.get(TlFeatureRevision, f.published_revision_id)
        new = await content.new_git_revision(db, f, old, sha, "legacy")
        old.status = TlRevisionStatus.SUPERSEDED
        f.published_revision_id = new.id
        f.alignment_pending = False
        f.status = TlFeatureStatus.RUNNING
        if with_draft:
            draft = TlFeatureRevision(
                feature_id=f.id, number=new.number + 1, status=TlRevisionStatus.DRAFT,
                title="bozza", created_by="t@example.test", base_revision_id=new.id,
            )
            db.add(draft)
            await db.flush()
            f.working_revision_id = draft.id
        await db.commit()
        return old.id, new.id


async def _scenario_rows(revision_id):
    async with TestSession() as db:
        return list(
            (
                await db.execute(
                    select(TlScenarioContent)
                    .where(TlScenarioContent.revision_id == revision_id)
                    .order_by(TlScenarioContent.position)
                )
            ).scalars()
        )


async def test_dry_run_reports_and_changes_nothing(env):
    s, _, git, _ = env
    fid = await _migrated(env)
    path = f"src/{s}/pay.feature"
    sha = git.seed_file(path, CHANGED)
    first, empty = await _wipe(fid, sha)
    async with TestSession() as db:
        report = await realign.realign_from_main(db, git, fid, apply=False)
    assert report.status == "ready" and report.scenarios == 2 and report.baseline_revision == 1
    async with TestSession() as db:
        feature = await db.get(TlFeature, fid)
        assert feature.published_revision_id == empty and not feature.alignment_pending


async def test_apply_rebuilds_the_hub_from_main_and_restores_the_alignment(env):
    s, _, git, _ = env
    fid = await _migrated(env)
    path = f"src/{s}/pay.feature"
    sha = git.seed_file(path, CHANGED)
    first, empty = await _wipe(fid, sha, with_draft=True)
    async with TestSession() as db:
        db.add(
            TlIssue(
                feature_id=fid, kind=TlIssueKind.STRUCTURAL_CHANGE, status=TlIssueStatus.OPEN,
                detail={"changes": []},
            )
        )
        await db.commit()
    async with TestSession() as db:
        report = await realign.realign_from_main(db, git, fid, apply=True)
    assert report.status == "applied"
    async with TestSession() as db:
        feature = await db.get(TlFeature, fid)
        assert feature.published_revision_id == first and feature.working_revision_id is None
        assert feature.alignment_pending
        rev = await db.get(TlFeatureRevision, first)
        assert rev.status == TlRevisionStatus.MERGED and rev.title == "Pagamento aggiornato"
        assert (await db.get(TlFeatureRevision, empty)).status == TlRevisionStatus.SUPERSEDED
        open_issues = (
            await db.execute(
                select(func.count()).select_from(TlIssue).where(
                    TlIssue.feature_id == fid, TlIssue.status == TlIssueStatus.OPEN
                )
            )
        ).scalar_one()
        assert open_issues == 0
        tags = [
            t for (t,) in (
                await db.execute(select(TlScenario.tag).where(TlScenario.feature_id == fid))
            ).all()
        ]
        assert len(tags) == 2 and all(t.startswith("@") for t in tags)
    steps = [c.steps for c in await _scenario_rows(first)]
    assert "Quando pago con carta" in steps[0]  # the branch version wins
    # the alignment pull request carries the file with the hub header and the main content
    async with TestSession() as db:
        out = await migration_align.open_alignment_pr(db, s, git)
    assert out.pr_error is None
    text = git.files(migration_align.branch_for(s))[path][0]
    assert text.startswith("# qa-hub: FEAT_001_") and "Quando pago con carta" in text


async def test_a_different_number_of_scenarios_is_left_to_a_person(env):
    s, _, git, _ = env
    fid = await _migrated(env)
    sha = git.seed_file(f"src/{s}/pay.feature", CHANGED + "\n  Scenario: Extra\n    Dato x\n")
    _, empty = await _wipe(fid, sha)
    async with TestSession() as db:
        report = await realign.realign_from_main(db, git, fid, apply=True)
    assert report.status == "skipped" and "scenari" in report.reason
    async with TestSession() as db:
        assert (await db.get(TlFeature, fid)).published_revision_id == empty


async def test_a_draft_with_content_is_never_deleted(env):
    s, _, git, _ = env
    fid = await _migrated(env)
    sha = git.seed_file(f"src/{s}/pay.feature", CHANGED)
    _, empty = await _wipe(fid, sha, with_draft=True)
    async with TestSession() as db:
        feature = await db.get(TlFeature, fid)
        scenario = (
            await db.execute(select(TlScenario).where(TlScenario.feature_id == fid))
        ).scalars().first()
        db.add(
            TlScenarioContent(
                revision_id=feature.working_revision_id, scenario_id=scenario.id, position=1,
                title="lavoro", steps="Dato x",
            )
        )
        await db.commit()
    async with TestSession() as db:
        report = await realign.realign_from_main(db, git, fid, apply=True)
    assert report.status == "skipped" and "bozza" in report.reason
    async with TestSession() as db:
        assert (await db.get(TlFeature, fid)).published_revision_id == empty

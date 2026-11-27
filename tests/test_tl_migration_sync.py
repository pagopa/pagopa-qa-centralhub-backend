# ruff: noqa: S101, F811
"""Fase 5, Task 7: chiusura della migrazione al merge della PR di allineamento."""

from __future__ import annotations

from sqlalchemy import func, select

from app.models.audit_log import AuditLog
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlIssueKind,
    TlFeatureStatus,
    TlFeatureVersion,
    TlIssue,
    TlMigrationScope,
    TlScenario,
    TlScope,
)
from app.services.test_lifecycle import git_import, git_sync, migration_align
from tests._db import TestSession
from tests.test_tl_features import _use_test_db, actor  # noqa: F401
from tests.test_tl_migration_execute import LEGACY, _analyze_confirm, _execute, env  # noqa: F401
from tests.tl_migration_fixtures import clean_migration  # noqa: F401


async def _feature(scope):
    async with TestSession() as db:
        return (
            await db.execute(
                select(TlFeature)
                .join(TlScope, TlScope.id == TlFeature.scope_id)
                .where(TlScope.code == scope, TlFeature.number == 1)
            )
        ).scalar_one()


async def _count(model, *where):
    async with TestSession() as db:
        return (
            await db.execute(select(func.count()).select_from(model).where(*where))
        ).scalar_one()


async def test_merge_clears_pending_without_issues(env):
    s, conf, git, _ = env
    await _analyze_confirm(conf, git, s)
    out = await _execute(env)
    sha = git.merge_pull(out.pr_number)
    path = f"src/{s}/pay.feature"
    async with TestSession() as db:
        await migration_align.sync_alignment_prs(db, git)
    async with TestSession() as db:
        await git_import.import_changed_file(db, path, None, sha, git)
    feat = await _feature(s)
    assert not feat.alignment_pending and feat.status == TlFeatureStatus.IMPLEMENTED
    assert await _count(TlIssue, TlIssue.feature_id == feat.id) == 0
    assert await _count(TlFeatureVersion, TlFeatureVersion.feature_id == feat.id) == 1
    assert (
        await _count(
            AuditLog, AuditLog.resource_id == str(feat.id), AuditLog.action == "tl.feature.aligned"
        )
        == 1
    )
    async with TestSession() as db:
        row = await db.get(TlMigrationScope, s)
    assert row.pr_state == "merged"


async def test_legacy_file_change_while_pending_is_ignored(env):
    s, conf, git, _ = env
    await _analyze_confirm(conf, git, s)
    await _execute(env)
    path = f"src/{s}/pay.feature"
    sha = git.seed_file(path, LEGACY.format(s=s) + "\n# ritocco\n")
    async with TestSession() as db:
        await git_import.import_changed_file(db, path, None, sha, git)
    feat = await _feature(s)
    assert feat.alignment_pending
    assert await _count(TlIssue, TlIssue.feature_id == feat.id) == 0
    assert await _count(TlIssue, TlIssue.path == path) == 0


async def test_closed_pr_records_error(env):
    s, conf, git, _ = env
    await _analyze_confirm(conf, git, s)
    out = await _execute(env)
    git.set_pull_state(out.pr_number, "closed")
    async with TestSession() as db:
        await migration_align.sync_alignment_prs(db, git)
    async with TestSession() as db:
        row = await db.get(TlMigrationScope, s)
    assert row.pr_state == "closed" and row.pr_error == "PR chiusa senza merge"
    assert (await _feature(s)).alignment_pending


async def test_run_sync_calls_alignment_check(monkeypatch):
    called = []

    async def spy(db, client):
        called.append(True)

    async def no_prs(db, client):
        return 0, 0

    monkeypatch.setattr(migration_align, "sync_alignment_prs", spy)
    monkeypatch.setattr(git_sync, "_sync_pull_requests", no_prs)

    class _Git:
        async def head_sha(self, branch):
            return "abc"

        async def compare(self, base, head):
            return []

    async def no_index(db, client, head):
        return None

    monkeypatch.setattr(git_sync, "rebuild_step_index", no_index)
    async with TestSession() as db:
        monkeypatch.setattr(git_sync, "_save_state", lambda db, s: _noop())
        await git_sync._run_locked(db, _Git())
    assert called == [True]


async def _noop():
    return None


async def test_merge_keeps_pending_a_feature_whose_file_the_pr_did_not_align(env):
    s, conf, git, _ = env
    await _analyze_confirm(conf, git, s)
    out = await _execute(env)
    path = f"src/{s}/pay.feature"
    # the pull request is merged, but its branch carries the file as it was (legacy)
    git.seed_file(path, LEGACY.format(s=s), branch=migration_align.branch_for(s))
    git.merge_pull(out.pr_number)
    async with TestSession() as db:
        await migration_align.sync_alignment_prs(db, git)
    feat = await _feature(s)
    assert feat.alignment_pending  # still waiting: main has no `# qa-hub:` header for it
    assert (
        await _count(
            AuditLog, AuditLog.resource_id == str(feat.id), AuditLog.action == "tl.feature.aligned"
        )
        == 0
    )
    async with TestSession() as db:
        row = await db.get(TlMigrationScope, s)
    assert row.pr_state == "merged" and path in row.pr_error


async def test_a_file_without_identity_never_empties_the_published_scenarios(env):
    s, conf, git, _ = env
    await _analyze_confirm(conf, git, s)
    out = await _execute(env)
    path = f"src/{s}/pay.feature"
    sha = git.merge_pull(out.pr_number)
    async with TestSession() as db:
        await migration_align.sync_alignment_prs(db, git)
    async with TestSession() as db:
        await git_import.import_changed_file(db, path, None, sha, git)
    before = await _feature(s)
    scenarios = await _count(TlScenario, TlScenario.feature_id == before.id)
    assert scenarios > 0 and not before.alignment_pending
    # later the file on main goes back to a version without header and identity tags
    legacy_sha = git.seed_file(path, LEGACY.format(s=s))
    async with TestSession() as db:
        await git_import.import_changed_file(db, path, None, legacy_sha, git)
    after = await _feature(s)
    assert after.published_revision_id == before.published_revision_id
    assert await _count(TlScenario, TlScenario.feature_id == after.id) == scenarios
    assert await _count(
        TlFeatureRevision, TlFeatureRevision.feature_id == after.id
    ) == await _count(TlFeatureRevision, TlFeatureRevision.feature_id == before.id)
    assert await _count(
        TlIssue, TlIssue.feature_id == after.id, TlIssue.kind == TlIssueKind.STRUCTURAL_CHANGE
    ) >= 1

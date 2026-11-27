# ruff: noqa: F811, S101
"""Migration alignment pull requests follow a move or a rename (design 2026-11-27)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.models.test_lifecycle import TlFeature, TlMigrationScope, TlScope
from app.models.user import User
from app.schemas.test_lifecycle import TlScopeUpdate
from app.services.test_lifecycle import migration_align, rekey, scopes
from tests._db import TestSession
from tests.test_tl_features import _use_test_db, actor  # noqa: F401
from tests.test_tl_migration_execute import (  # noqa: F401  (fixture e helper riusati)
    LEGACY,
    REPO,
    _analyze_confirm,
    _execute,
    env,
)
from tests.tl_migration_fixtures import clean_migration, page  # noqa: F401


@pytest.fixture
async def dest():
    code = "D" + uuid.uuid4().hex[:8]
    async with TestSession() as s:
        sc = TlScope(code=code, name="WISP", repo_folder="src/integration/wisp")
        s.add(sc)
        await s.commit()
        scope_id = sc.id
    yield {"id": scope_id, "code": code}
    async with TestSession() as s:
        await s.execute(delete(TlMigrationScope).where(TlMigrationScope.scope_code == code))
        await s.execute(delete(TlScope).where(TlScope.id == scope_id))
        await s.commit()


async def _row(code):
    async with TestSession() as s:
        return (
            await s.execute(
                select(TlMigrationScope).where(TlMigrationScope.scope_code == code)
            )
        ).scalar_one_or_none()


async def _migrated(env):
    s, conf, git, actor = env
    await _analyze_confirm(conf, git, s)
    await _execute(env)
    async with TestSession() as db:
        scope = (await db.execute(select(TlScope).where(TlScope.code == s))).scalar_one()
        feats = {
            f.number: f
            for f in (
                await db.execute(select(TlFeature).where(TlFeature.scope_id == scope.id))
            ).scalars()
        }
        return scope.id, {n: f.id for n, f in feats.items()}


async def _move(feature_ids, dest, actor_email):
    async with TestSession() as db:
        user = (await db.execute(select(User).where(User.email == actor_email))).scalar_one()
        moved, _ = await rekey.move_features(db, feature_ids, dest["id"], user)
    return moved


async def test_close_pull_on_the_fake(env):
    _, _, git, _ = env
    git.branches["b"] = git.branches["main"]
    pull = await git.create_pull("b", "main", "T", "B")
    await git.close_pull(pull.number)
    assert git.pulls[pull.number].state == "closed"


async def test_a_moved_feature_goes_to_the_destination_pr_and_the_empty_source_pr_closes(
    env, dest
):
    s, conf, git, actor = env
    _, ids = await _migrated(env)
    source_row = await _row(s)
    assert source_row.pr_state == "open"
    source_pr, source_branch = source_row.pr_number, migration_align.branch_for(s)
    path = f"src/{s}/pay.feature"
    moved = await _move([ids[1]], dest, actor["email"])
    new_code = moved[0].new_code
    assert new_code.endswith(dest["code"])

    async with TestSession() as db:
        error = await migration_align.refresh_after_change(db, git, [(s, dest["code"])])
    assert error is None

    dest_branch = migration_align.branch_for(dest["code"])
    dest_row = await _row(dest["code"])
    assert dest_row.pr_state == "open" and dest_row.pr_number is not None
    assert f"# qa-hub: {new_code}" in git.files(dest_branch)[path][0]
    # the source branch carries the file as it is on main again, and its PR is closed
    assert git.files(source_branch)[path][0] == git.files("main")[path][0]
    assert git.pulls[source_pr].state == "closed"
    assert (await _row(s)).pr_state == "closed"


async def test_a_source_pr_that_still_has_features_is_refreshed_not_closed(env, dest):
    s, conf, git, actor = env
    second = f"src/{s}/other.feature"
    git.seed_file(second, LEGACY.format(s=s).replace("FEAT_001", "FEAT_003"))
    conf.add_page(
        "p3", f"FEAT_003_{s} - Altra",
        page(f"FEAT_003_{s}", "Altra", "test suite implemented", file_url=REPO + second),
    )
    _, ids = await _migrated(env)
    source_pr = (await _row(s)).pr_number
    await _move([ids[1]], dest, actor["email"])
    async with TestSession() as db:
        assert await migration_align.refresh_after_change(db, git, [(s, dest["code"])]) is None
    assert git.pulls[source_pr].state == "open"
    branch = migration_align.branch_for(s)
    assert "# qa-hub:" in git.files(branch)[second][0]  # the remaining feature is still carried
    pay = f"src/{s}/pay.feature"
    assert git.files(branch)[pay][0] == git.files("main")[pay][0]


async def test_a_git_failure_is_reported_and_the_move_stays(env, dest):
    s, conf, git, actor = env
    _, ids = await _migrated(env)
    await _move([ids[1]], dest, actor["email"])
    git.fail_once("put_file", 502, "GitHub giù")
    async with TestSession() as db:
        error = await migration_align.refresh_after_change(db, git, [(s, dest["code"])])
    assert error and "GitHub giù" in error
    async with TestSession() as db:
        feature = await db.get(TlFeature, ids[1])
        assert feature.scope_id == dest["id"]


async def test_without_a_token_nothing_is_called(env, dest):
    s, _, _, actor = env
    _, ids = await _migrated(env)
    await _move([ids[1]], dest, actor["email"])
    async with TestSession() as db:
        error = await migration_align.refresh_after_change(db, None, [(s, dest["code"])])
    assert error and "Git" in error


async def test_a_scope_rename_refreshes_the_same_way(env):
    s, conf, git, actor = env
    scope_id, _ = await _migrated(env)
    source_pr = (await _row(s)).pr_number
    new = "N" + uuid.uuid4().hex[:8]
    try:
        async with TestSession() as db:
            await scopes.update_scope(db, scope_id, TlScopeUpdate(code=new))
        async with TestSession() as db:
            assert await migration_align.refresh_after_change(db, git, [(s, new)]) is None
        assert (await _row(new)).pr_state == "open"
        assert git.pulls[source_pr].state == "closed"
        assert f"# qa-hub: FEAT_001_{new}" in git.files(migration_align.branch_for(new))[
            f"src/{s}/pay.feature"
        ][0]
    finally:
        async with TestSession() as db:
            await db.execute(delete(TlMigrationScope).where(TlMigrationScope.scope_code == new))
            await db.commit()

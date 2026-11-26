# ruff: noqa: F811, S101
"""Working-branch sync: status and push (design 2026-11-26, git sync)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlRevisionStatus,
    TlScope,
)
from app.services.test_lifecycle import branch_sync, history
from app.services.test_lifecycle.actors import resolve_actor
from app.services.test_lifecycle.branch_sync_rules import blob_sha
from app.services.test_lifecycle.gherkin import render_feature_file
from app.services.test_lifecycle.git_publish import commit_message, revision_file_path
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _new_scenario,
    _use_test_db,
    actor,
    scope,
)
from tests.test_tl_git_import import _events, fake  # noqa: F401

BRANCH = "qa-hub/work-r1"


async def _setup(client, scope, actor, *, branch=BRANCH):
    """A draft with one scenario; `work_branch` set in the database (nothing on Git yet)."""
    feat = await _new_feature(client, scope, actor)
    await _new_scenario(client, feat, actor)
    rev_id = uuid.UUID(feat["working_revision"]["id"])
    if branch is not None:
        async with TestSession() as s:
            await s.execute(
                update(TlFeatureRevision)
                .where(TlFeatureRevision.id == rev_id)
                .values(work_branch=branch)
            )
            await s.commit()
    return uuid.UUID(feat["id"]), rev_id, feat


async def _status(fake, feature_id, rev_id):
    async with TestSession() as s:
        return await branch_sync.get_status(s, feature_id, rev_id, fake)


async def _push(fake, feature_id, rev_id, actor, **kw):
    async with TestSession() as s:
        user = await resolve_actor(s, actor["email"])
        return await branch_sync.push(s, feature_id, rev_id, user, fake, **kw)


async def _render_text(feature_id, rev_id):
    async with TestSession() as s:
        feature = await s.get(TlFeature, feature_id)
        rev = await s.get(TlFeatureRevision, rev_id)
        render, _ = await history.build_render(s, feature, rev)
        return render_feature_file(render)


async def _path(feature_id, rev_id):
    async with TestSession() as s:
        feature = await s.get(TlFeature, feature_id)
        rev = await s.get(TlFeatureRevision, rev_id)
        scope = await s.get(TlScope, feature.scope_id)
        return revision_file_path(scope, feature, rev)


async def _rev(rev_id):
    async with TestSession() as s:
        return await s.get(TlFeatureRevision, rev_id)


async def _edit(client, feature_id, actor, steps):
    detail = (await client.get(f"{BASE}/features/{feature_id}")).json()
    rev = detail["working_revision"]
    resp = await client.patch(
        f"{BASE}/features/{feature_id}/revisions/{rev['id']}/scenarios/{rev['scenarios'][0]['id']}",
        json={"lock_version": rev["lock_version"], "steps": steps, "actor_email": actor["email"]},
    )
    assert resp.status_code == 200, resp.text


STEPS_A = "Dato un carrello\nQuando paga\nAllora vede la conferma"
STEPS_B = "Dato un carrello\nQuando paga in contanti\nAllora vede la conferma"


# ── status ───────────────────────────────────────────────────────────────────


async def test_status_without_a_working_branch(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor, branch=None)
    out = await _status(fake, fid, rid)
    assert (out.branch, out.state) == (None, "no_branch")
    assert fake.calls == []


async def test_status_branch_missing(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    out = await _status(fake, fid, rid)
    assert (out.branch, out.state) == (BRANCH, "branch_missing")


async def test_status_file_missing_when_the_branch_has_no_file(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    fake.branches[BRANCH] = fake.branches["main"]
    out = await _status(fake, fid, rid)
    assert out.state == "file_missing" and out.hub_unsynced is True


async def test_push_then_status_is_in_sync(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    await _edit(client, fid, actor, STEPS_A)
    out = await _push(fake, fid, rid, actor)
    assert out.state == "in_sync" and out.hub_unsynced is False and out.repo_changed is False
    path, text = await _path(fid, rid), await _render_text(fid, rid)
    assert fake.files(BRANCH)[path][0] == text
    rev = await _rev(rid)
    assert rev.work_file_sha == blob_sha(text) and rev.work_hub_sha == blob_sha(text)
    assert (await _status(fake, fid, rid)).state == "in_sync"


async def test_status_hub_ahead_after_a_hub_edit(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    await _edit(client, fid, actor, STEPS_A)
    await _push(fake, fid, rid, actor)
    await _edit(client, fid, actor, STEPS_B)
    out = await _status(fake, fid, rid)
    assert out.state == "hub_ahead" and out.hub_unsynced is True and out.repo_changed is False


async def test_status_repo_ahead_after_a_commit_on_the_branch(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    await _edit(client, fid, actor, STEPS_A)
    await _push(fake, fid, rid, actor)
    path = await _path(fid, rid)
    fake.seed_file(path, (await _render_text(fid, rid)) + "# dev\n", branch=BRANCH)
    out = await _status(fake, fid, rid)
    assert out.state == "repo_ahead" and out.repo_changed is True and out.hub_unsynced is False


async def test_status_diverged_when_both_changed(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    await _edit(client, fid, actor, STEPS_A)
    await _push(fake, fid, rid, actor)
    path = await _path(fid, rid)
    fake.seed_file(path, (await _render_text(fid, rid)) + "# dev\n", branch=BRANCH)
    await _edit(client, fid, actor, STEPS_B)
    assert (await _status(fake, fid, rid)).state == "diverged"


async def test_both_sides_changed_to_the_same_content_is_in_sync(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    await _edit(client, fid, actor, STEPS_A)
    await _push(fake, fid, rid, actor)
    await _edit(client, fid, actor, STEPS_B)
    fake.seed_file(await _path(fid, rid), await _render_text(fid, rid), branch=BRANCH)
    assert (await _status(fake, fid, rid)).state == "in_sync"


async def test_status_unreachable_and_not_configured(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    fake.fail_once("branch_exists", 502, "bad gateway")
    assert (await _status(fake, fid, rid)).state == "unreachable"
    assert (await _status(branch_sync.NoGitClient(), fid, rid)).state == "not_configured"


async def test_status_never_writes(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    await _status(fake, fid, rid)
    fake.branches[BRANCH] = fake.branches["main"]
    await _status(fake, fid, rid)
    for method in ("put_file", "create_branch", "reset_branch", "delete_file", "create_pull"):
        assert fake.calls_of(method) == []


# ── push ─────────────────────────────────────────────────────────────────────


async def test_push_creates_a_missing_branch_from_the_base_head(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    base_head = fake.branches["main"]
    await _push(fake, fid, rid, actor)
    assert fake.calls_of("create_branch") == [("create_branch", BRANCH, base_head)]
    assert await _path(fid, rid) in fake.files(BRANCH)


async def test_push_writes_only_the_feature_file_with_the_publish_message(
    client, scope, actor, fake
):
    fid, rid, _ = await _setup(client, scope, actor)
    fake.branches[BRANCH] = fake.branches["main"]
    fake.seed_file("src/steps/pay_steps.py", "def pay(): ...\n", branch=BRANCH)
    developer_commit = fake.branches[BRANCH]
    await _push(fake, fid, rid, actor)
    path = await _path(fid, rid)
    assert set(fake.files(BRANCH)) == set(fake.files("main")) | {path, "src/steps/pay_steps.py"}
    assert [c[1] for c in fake.calls_of("put_file")] == [path]
    async with TestSession() as s:
        feature = await s.get(TlFeature, fid)
        rev = await s.get(TlFeatureRevision, rid)
        scope_row = await s.get(TlScope, feature.scope_id)
        assert fake.put_messages == [commit_message(scope_row, feature, rev)]
    assert fake.branches[BRANCH] != developer_commit  # one commit on top of the developer's
    assert fake.calls_of("reset_branch") == []


async def test_a_second_identical_push_makes_no_commit(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    await _push(fake, fid, rid, actor)
    head = fake.branches[BRANCH]
    await _push(fake, fid, rid, actor)
    assert len(fake.calls_of("put_file")) == 1 and fake.branches[BRANCH] == head


async def test_push_refuses_to_overwrite_a_changed_repository(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    await _edit(client, fid, actor, STEPS_A)
    await _push(fake, fid, rid, actor)
    path = await _path(fid, rid)
    dev_text = (await _render_text(fid, rid)) + "# dev\n"
    fake.seed_file(path, dev_text, branch=BRANCH)
    await _edit(client, fid, actor, STEPS_B)
    puts = len(fake.calls_of("put_file"))
    with pytest.raises(branch_sync.RepoChangedError):
        await _push(fake, fid, rid, actor)
    assert len(fake.calls_of("put_file")) == puts and fake.files(BRANCH)[path][0] == dev_text

    out = await _push(fake, fid, rid, actor, overwrite=True)
    assert out.state == "in_sync"
    assert fake.files(BRANCH)[path][0] == await _render_text(fid, rid)


async def test_push_needs_a_draft_with_a_working_branch(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == rid)
            .values(status=TlRevisionStatus.IN_REVIEW)
        )
        await s.commit()
    with pytest.raises(branch_sync.SyncNotAllowedError):
        await _push(fake, fid, rid, actor)
    fid2, rid2, _ = await _setup(client, scope, actor, branch=None)
    with pytest.raises(branch_sync.SyncNotAllowedError):
        await _push(fake, fid2, rid2, actor)
    assert fake.calls_of("put_file") == []


async def test_push_does_not_adopt_the_path_and_records_an_event(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    await _push(fake, fid, rid, actor)
    async with TestSession() as s:
        assert (await s.get(TlFeature, fid)).git_path is None
    events = await _events(fid, "tl.git.pushed")
    assert len(events) == 1 and events[0]["branch"] == BRANCH


async def test_a_busy_revision_is_not_pushed_twice_at_once(client, scope, actor, fake):
    import asyncio

    fid, rid, _ = await _setup(client, scope, actor)
    started, release = fake.block("put_file")
    first = asyncio.create_task(_push(fake, fid, rid, actor))
    await started.wait()
    with pytest.raises(branch_sync.SyncNotAllowedError):
        await _push(fake, fid, rid, actor)
    release.set()
    await first


# ── review fixes: two shas, a push that outlives its revision's draft state ──────


async def test_a_crlf_copy_of_the_file_is_not_a_divergence(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    await _edit(client, fid, actor, STEPS_A)
    await _push(fake, fid, rid, actor)
    path = await _path(fid, rid)
    crlf = fake.files(BRANCH)[path][0].replace("\n", "\r\n")
    fake.seed_file(path, crlf, branch=BRANCH)
    assert (await _status(fake, fid, rid)).state == "in_sync"
    await _push(fake, fid, rid, actor)  # same content: nothing to write, but the repo sha is noted
    assert fake.files(BRANCH)[path][0] == crlf
    await _edit(client, fid, actor, STEPS_B)
    assert (await _status(fake, fid, rid)).state == "hub_ahead"  # not "diverged"
    out = await _push(fake, fid, rid, actor)  # no overwrite needed: nobody touched the repository
    assert out.state == "in_sync"


async def test_a_reformatted_file_accepted_from_the_repo_is_in_sync(client, scope, actor, fake):
    from app.models.test_lifecycle import TlFeatureRevision  # noqa: F401
    from app.services.test_lifecycle import branch_sync as svc
    from app.services.test_lifecycle.actors import resolve_actor

    fid, rid, _ = await _setup(client, scope, actor)
    await _edit(client, fid, actor, STEPS_A)
    await _push(fake, fid, rid, actor)
    path = await _path(fid, rid)
    text = fake.files(BRANCH)[path][0]
    reformatted = text.replace("\n  @", "\n\n  @", 1)  # an extra blank line the hub never writes
    assert reformatted != text
    fake.seed_file(path, reformatted, branch=BRANCH)
    assert (await _status(fake, fid, rid)).state == "repo_ahead"
    async with TestSession() as s:
        user = await resolve_actor(s, actor["email"])
        await svc.pull(s, fid, rid, user, fake)
    assert (await _status(fake, fid, rid)).state == "in_sync"
    fake.seed_file(path, reformatted + "# dev\n", branch=BRANCH)
    assert (await _status(fake, fid, rid)).state == "repo_ahead"  # not "diverged"


async def test_a_push_stores_its_sha_even_if_the_revision_left_draft_meanwhile(
    client, scope, actor, fake
):
    import asyncio

    fid, rid, _ = await _setup(client, scope, actor)
    started, release = fake.block("put_file")
    task = asyncio.create_task(_push(fake, fid, rid, actor))
    await started.wait()
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == rid)
            .values(status=TlRevisionStatus.IN_REVIEW)
        )
        await s.commit()
    release.set()
    await task  # the commit exists: it is recorded, not refused
    text, path = await _render_text(fid, rid), await _path(fid, rid)
    rev = await _rev(rid)
    assert fake.files(BRANCH)[path][0] == text
    assert rev.work_file_sha == blob_sha(text) and rev.work_hub_sha == blob_sha(text)

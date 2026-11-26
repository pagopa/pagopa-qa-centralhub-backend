# ruff: noqa: F811, S101
"""Submit pushes the draft first; reopen opens a working branch (design 2026-11-26, git sync)."""
from __future__ import annotations

import uuid

from sqlalchemy import update

from app.models.test_lifecycle import TlFeature, TlFeatureRevision
from app.services.test_lifecycle import history
from app.services.test_lifecycle.branch_sync_rules import blob_sha
from app.services.test_lifecycle.gherkin import render_feature_file
from tests._db import TestSession
from tests.test_tl_branch_sync import _path, _rev  # noqa: F401
from tests.test_tl_features import BASE, _use_test_db, actor, scope  # noqa: F401
from tests.test_tl_git_import import _merged, fake  # noqa: F401
from tests.test_tl_workflow import _do, _get, _ready_feature, reviewer  # noqa: F401

REASON = "Il repo è cambiato: accetta o sovrascrivi"
WORK = "qa-hub/ready-r1"


async def _reopen(client, done, actor, **extra):
    published = (await _get(client, done["id"], actor["email"]))["published_revision"]
    url = f"{BASE}/features/{done['id']}/revisions/{published['id']}/transitions"
    body = {"action": "reopen", "actor_email": actor["email"], **extra}
    return await client.post(url, json=body), published


async def _published_sha(published_id, feature_id):
    async with TestSession() as s:
        feature = await s.get(TlFeature, uuid.UUID(feature_id))
        rev = await s.get(TlFeatureRevision, uuid.UUID(published_id))
        render, _ = await history.build_render(s, feature, rev)
        return blob_sha(render_feature_file(render))


# ── reopen ───────────────────────────────────────────────────────────────────


async def test_reopen_opens_the_default_branch_without_writing_a_file(
    client, scope, actor, reviewer, fake
):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    puts = len(fake.calls_of("put_file"))
    main_head = fake.branches["main"]
    resp, published = await _reopen(client, done, actor)
    assert resp.status_code == 200, resp.text
    rev = resp.json()["working_revision"]
    branch = f"qa-hub/{done['code']}-r2"
    assert rev["work_branch"] == branch
    assert fake.branches[branch] == main_head
    assert len(fake.calls_of("put_file")) == puts
    saved = await _rev(uuid.UUID(rev["id"]))
    assert saved.work_hub_sha == await _published_sha(published["id"], done["id"])
    assert saved.work_file_sha == fake.files("main")[done["git_path"]][1]  # main's file as it is


async def test_reopen_with_a_custom_branch_name(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    resp, _ = await _reopen(client, done, actor, branch_name="feature/pos-fix")
    assert resp.status_code == 200, resp.text
    assert resp.json()["working_revision"]["work_branch"] == "feature/pos-fix"
    assert "feature/pos-fix" in fake.branches


async def test_reopen_reuses_an_existing_branch_untouched(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    path = done["git_path"]
    fake.branches["feature/dev"] = fake.branches["main"]
    fake.seed_file("src/steps/pay.py", "def pay(): ...\n", branch="feature/dev")
    fake.seed_file(path, fake.files("main")[path][0] + "# dev\n", branch="feature/dev")
    head = fake.branches["feature/dev"]
    resp, _ = await _reopen(client, done, actor, branch_name="feature/dev")
    assert resp.status_code == 200, resp.text
    assert fake.branches["feature/dev"] == head
    assert fake.calls_of("create_branch") == [
        c for c in fake.calls_of("create_branch") if c[1] != "feature/dev"
    ]
    rev = resp.json()["working_revision"]
    status = await client.get(f"{BASE}/features/{done['id']}/revisions/{rev['id']}/git-sync")
    assert status.json()["state"] == "repo_ahead"


async def test_reopen_with_an_invalid_branch_name_is_422_and_changes_nothing(
    client, scope, actor, reviewer, fake
):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    for name in ("main", "a..b", "a b", ""):
        resp, _ = await _reopen(client, done, actor, branch_name=name)
        assert resp.status_code == 422, name
        assert resp.json()["detail"]["code"] == "invalid_branch_name"
    assert (await _get(client, done["id"], actor["email"]))["working_revision"] is None


async def test_reopen_still_works_when_git_is_down(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    fake.fail_once("branch_exists", 502, "bad gateway")
    resp, _ = await _reopen(client, done, actor)
    assert resp.status_code == 200, resp.text
    rev = resp.json()["working_revision"]
    assert rev["work_branch"] == f"qa-hub/{done['code']}-r2"
    assert rev["work_branch"] not in fake.branches
    status = await client.get(f"{BASE}/features/{done['id']}/revisions/{rev['id']}/git-sync")
    assert status.json()["state"] == "branch_missing"


# ── submit ───────────────────────────────────────────────────────────────────


async def _ready_with_branch(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    rev_id = uuid.UUID(detail["working_revision"]["id"])
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == rev_id)
            .values(work_branch=WORK)
        )
        await s.commit()
    return detail, rev_id


async def _sync_url(detail, suffix=""):
    rev = detail["working_revision"]["id"]
    return f"{BASE}/features/{detail['id']}/revisions/{rev}/git-sync{suffix}"


async def test_submit_pushes_the_draft_to_its_branch_first(client, scope, actor, reviewer, fake):
    detail, rev_id = await _ready_with_branch(client, scope, actor, reviewer)
    resp = await _do(client, detail, "submit", actor["email"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["working_revision"]["status"] == "in_review"
    assert await _path(uuid.UUID(detail["id"]), rev_id) in fake.files(WORK)


async def _push_then_change_repo(client, detail, actor, fake, rev_id):
    url = await _sync_url(detail, "/push")
    assert (await client.post(url, json={"actor_email": actor["email"]})).status_code == 200
    path = await _path(uuid.UUID(detail["id"]), rev_id)
    fake.seed_file(path, fake.files(WORK)[path][0] + "# dev\n", branch=WORK)


async def test_submit_is_blocked_when_the_repository_changed(
    client, scope, actor, reviewer, fake
):
    detail, rev_id = await _ready_with_branch(client, scope, actor, reviewer)
    await _push_then_change_repo(client, detail, actor, fake, rev_id)
    resp = await _do(client, detail, "submit", actor["email"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "transition_not_allowed"
    assert resp.json()["detail"]["reasons"] == [REASON]
    assert (await _get(client, detail["id"]))["working_revision"]["status"] == "draft"


async def test_submit_is_blocked_when_both_sides_changed(client, scope, actor, reviewer, fake):
    detail, rev_id = await _ready_with_branch(client, scope, actor, reviewer)
    await _push_then_change_repo(client, detail, actor, fake, rev_id)
    rev = detail["working_revision"]
    resp = await client.patch(
        f"{BASE}/features/{detail['id']}/revisions/{rev['id']}"
        f"/scenarios/{rev['scenarios'][0]['id']}",
        json={
            "lock_version": (await _get(client, detail["id"]))["working_revision"]["lock_version"],
            "steps": "Dato un carrello\nQuando paga in contanti\nAllora vede la conferma",
            "actor_email": actor["email"],
        },
    )
    assert resp.status_code == 200, resp.text
    resp = await _do(client, detail, "submit", actor["email"])
    assert resp.status_code == 409 and resp.json()["detail"]["reasons"] == [REASON]


async def test_submit_is_not_blocked_by_a_git_outage(client, scope, actor, reviewer, fake):
    detail, _ = await _ready_with_branch(client, scope, actor, reviewer)
    fake.fail_once("branch_exists", 502, "bad gateway")
    resp = await _do(client, detail, "submit", actor["email"])
    assert resp.status_code == 200, resp.text


async def test_submit_is_not_blocked_without_a_token(client, scope, actor, reviewer):
    detail, _ = await _ready_with_branch(client, scope, actor, reviewer)
    resp = await _do(client, detail, "submit", actor["email"])
    assert resp.status_code == 200, resp.text


async def test_submit_without_a_working_branch_makes_no_git_call(
    client, scope, actor, reviewer, fake
):
    detail = await _ready_feature(client, scope, actor, reviewer)
    resp = await _do(client, detail, "submit", actor["email"])
    assert resp.status_code == 200, resp.text
    assert fake.calls == []


async def test_reopen_is_in_sync_when_main_holds_the_file_in_another_format(
    client, scope, actor, reviewer, fake
):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    path = done["git_path"]
    text = fake.files("main")[path][0]
    fake.seed_file(path, text.replace("\n  @", "\n\n  @", 1))  # same content, other bytes
    resp, _ = await _reopen(client, done, actor)
    assert resp.status_code == 200, resp.text
    rev = resp.json()["working_revision"]
    status = await client.get(f"{BASE}/features/{done['id']}/revisions/{rev['id']}/git-sync")
    assert status.json()["state"] == "in_sync"


# ── Riapri cannot pick a shared branch or someone else's ────────────────────────


async def test_reopen_refuses_shared_and_protected_branch_names(
    client, scope, actor, reviewer, fake
):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    for name in ("develop", "master", "release/1.0", "_wip", "HEAD"):
        resp, _ = await _reopen(client, done, actor, branch_name=name)
        assert resp.status_code == 422, name
        assert resp.json()["detail"]["code"] == "invalid_branch_name"
    assert (await _get(client, done["id"], actor["email"]))["working_revision"] is None


async def test_reopen_refuses_the_branch_of_another_open_revision(
    client, scope, actor, reviewer, fake
):
    other, _ = await _ready_with_branch(client, scope, actor, reviewer)
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    resp, _ = await _reopen(client, done, actor, branch_name=WORK)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_branch_name"
    assert other["id"] != done["id"]


# ── submit while a push is running ───────────────────────────────────────────────


async def test_submit_while_a_push_is_running_is_refused_with_a_clear_reason(
    client, scope, actor, reviewer, fake
):
    import asyncio

    from app.services.test_lifecycle import branch_sync
    from app.services.test_lifecycle.actors import resolve_actor

    detail, rev_id = await _ready_with_branch(client, scope, actor, reviewer)
    started, release = fake.block("put_file")

    async def push():
        async with TestSession() as s:
            user = await resolve_actor(s, actor["email"])
            return await branch_sync.push(s, uuid.UUID(detail["id"]), rev_id, user, fake)

    task = asyncio.create_task(push())
    await started.wait()
    resp = await _do(client, detail, "submit", actor["email"])
    assert resp.status_code == 409
    reasons = resp.json()["detail"]["reasons"]
    assert reasons == ["Sincronizzazione con Git in corso: riprova tra poco"]
    assert (await _get(client, detail["id"]))["working_revision"]["status"] == "draft"
    release.set()
    await task
    assert (await _rev(rev_id)).work_file_sha is not None

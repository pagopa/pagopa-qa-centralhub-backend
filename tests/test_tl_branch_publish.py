# ruff: noqa: F811, S101
"""Publish uses the working branch and never resets it (design 2026-11-26, git sync)."""
from __future__ import annotations

import uuid

from sqlalchemy import update

from app.models.test_lifecycle import TlFeatureRevision
from app.services.test_lifecycle.branch_sync_rules import blob_sha
from tests._db import TestSession
from tests.test_tl_branch_sync import _path, _rev  # noqa: F401
from tests.test_tl_features import BASE, _use_test_db, actor, scope  # noqa: F401
from tests.test_tl_git_publish import _approved, fake  # noqa: F401
from tests.test_tl_workflow import _do, _get, _ready_feature, reviewer  # noqa: F401

WORK = "qa-hub/dev-work"
DEV_FILE = "src/steps/pay_steps.py"


async def _approve_with_branch(client, scope, actor, reviewer, fake, *, seed=True):
    detail = await _ready_feature(client, scope, actor, reviewer)
    rev_id = uuid.UUID(detail["working_revision"]["id"])
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision).where(TlFeatureRevision.id == rev_id).values(work_branch=WORK)
        )
        await s.commit()
    if seed:
        fake.branches[WORK] = fake.branches["main"]
        fake.seed_file(DEV_FILE, "def pay(): ...\n", branch=WORK)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    resp = await _do(client, out, "approve", reviewer["email"])
    assert resp.status_code == 200, resp.text
    return resp.json(), rev_id


async def _reapprove(client, detail, reviewer):
    out = (await _do(client, detail, "withdraw_approval", reviewer["email"])).json()
    resp = await _do(client, out, "approve", reviewer["email"])
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_publish_opens_the_pr_from_the_working_branch_and_keeps_developer_code(
    client, scope, actor, reviewer, fake
):
    done, rev_id = await _approve_with_branch(client, scope, actor, reviewer, fake)
    rev = done["working_revision"]
    path = await _path(uuid.UUID(done["id"]), rev_id)
    assert rev["pr_state"] == "open" and rev["work_branch"] == WORK
    assert [c[1] for c in fake.calls_of("create_pull")] == [WORK]
    assert path in fake.files(WORK) and DEV_FILE in fake.files(WORK)
    assert fake.calls_of("reset_branch") == []
    assert (await _rev(rev_id)).pr_branch == WORK


async def test_reapproval_never_resets_the_branch(client, scope, actor, reviewer, fake):
    done, rev_id = await _approve_with_branch(client, scope, actor, reviewer, fake)
    fake.seed_file("src/steps/more_steps.py", "def more(): ...\n", branch=WORK)
    await _reapprove(client, done, reviewer)
    files = fake.files(WORK)
    assert DEV_FILE in files and "src/steps/more_steps.py" in files
    assert fake.calls_of("reset_branch") == []
    assert len(fake.calls_of("create_pull")) == 1


async def test_a_file_edited_by_a_developer_is_left_as_it_is(client, scope, actor, reviewer, fake):
    done, rev_id = await _approve_with_branch(client, scope, actor, reviewer, fake)
    path = await _path(uuid.UUID(done["id"]), rev_id)
    edited = fake.files(WORK)[path][0] + "# dev note\n"
    fake.seed_file(path, edited, branch=WORK)
    puts = len(fake.calls_of("put_file"))
    again = await _reapprove(client, done, reviewer)
    assert fake.files(WORK)[path][0] == edited
    assert len(fake.calls_of("put_file")) == puts
    assert again["working_revision"]["pr_state"] == "open"
    assert again["working_revision"]["pr_error"] is None


async def test_a_hub_born_revision_gets_its_working_branch_at_approval(
    client, scope, actor, reviewer, fake
):
    done = await _approved(client, scope, actor, reviewer)
    rev_id = uuid.UUID(done["working_revision"]["id"])
    rev = await _rev(rev_id)
    branch = f"qa-hub/{done['code']}-r1"
    assert rev.work_branch == branch == rev.pr_branch
    path = done["git_path"]
    assert rev.work_file_sha == blob_sha(fake.files(branch)[path][0])
    assert fake.calls_of("reset_branch") == []


async def test_base_commit_sha_is_set_with_the_branch_and_not_moved_by_a_republish(
    client, scope, actor, reviewer, fake
):
    done = await _approved(client, scope, actor, reviewer)
    rev_id = uuid.UUID(done["working_revision"]["id"])
    first = (await _rev(rev_id)).base_commit_sha
    assert first
    fake.seed_file("docs/other.md", "x\n")  # main moves on
    await _reapprove(client, done, reviewer)
    assert (await _rev(rev_id)).base_commit_sha == first


async def test_a_republish_after_a_failed_pr_creation_writes_the_new_content(
    client, scope, actor, reviewer, fake
):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    fake.fail_once("create_pull", 502, "GitHub giù")
    resp = await _do(client, out, "approve", reviewer["email"])
    assert resp.status_code == 200, resp.text
    failed = resp.json()
    assert failed["working_revision"]["pr_state"] is None and failed["working_revision"]["pr_error"]
    branch = f"qa-hub/{failed['code']}-r1"
    assert branch in fake.branches  # the first attempt did create the branch and the file

    back = (await _do(client, failed, "withdraw_approval", reviewer["email"])).json()
    draft = (await _do(client, back, "withdraw", actor["email"])).json()
    rev = draft["working_revision"]
    scen = rev["scenarios"][0]
    edited = await client.patch(
        f"{BASE}/features/{draft['id']}/revisions/{rev['id']}/scenarios/{scen['id']}",
        json={
            "lock_version": rev["lock_version"],
            "steps": "Dato un carrello\nQuando paga in contanti\nAllora vede la conferma",
            "actor_email": actor["email"],
        },
    )
    assert edited.status_code == 200, edited.text
    again = (await _do(client, await _get(client, draft["id"]), "submit", actor["email"])).json()
    done = await _do(client, again, "approve", reviewer["email"])
    assert done.status_code == 200, done.text
    assert done.json()["working_revision"]["pr_state"] == "open"
    path = done.json()["git_path"]
    assert "Quando paga in contanti" in fake.files(branch)[path][0]

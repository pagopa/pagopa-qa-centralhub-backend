# ruff: noqa: F811, S101
"""Import opens a working branch and no cleanup PR (design 2026-11-26, git sync)."""
from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.services.test_lifecycle.branch_sync_rules import blob_sha
from tests._db import TestSession
from tests.test_tl_branch_sync import _rev
from tests.test_tl_features import BASE, _use_test_db, actor, scope  # noqa: F401
from tests.test_tl_import_git import BRANCH, FILE, PATH, REPO_NAME, URL
from tests.test_tl_workflow import _do, _get, _ready_feature, reviewer  # noqa: F401
from tests.tl_fakes import FakeGitClient, install_fake_git


@pytest.fixture
def git(monkeypatch):
    monkeypatch.setattr(settings, "tl_repo", REPO_NAME)
    monkeypatch.setattr(settings, "tl_base_branch", "main")
    fake = install_fake_git(monkeypatch, FakeGitClient())
    fake.branches[BRANCH] = fake.branches["main"]
    fake.seed_file(PATH, FILE, branch=BRANCH)
    return fake


async def _import(client, scope, actor, reviewer, url=URL):
    detail = await _ready_feature(client, scope, actor, reviewer)
    rev = detail["working_revision"]
    resp = await client.post(
        f"{BASE}/features/{detail['id']}/revisions/{rev['id']}/import-git",
        json={"actor_email": actor["email"], "lock_version": rev["lock_version"], "url": url},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["feature"]


async def test_import_opens_the_working_branch_with_the_file_at_its_path(
    client, scope, actor, reviewer, git
):
    source_head, main_head = git.branches[BRANCH], git.branches["main"]
    feature = await _import(client, scope, actor, reviewer)
    rev = feature["working_revision"]
    branch = f"qa-hub/{feature['code']}-r1"
    assert rev["work_branch"] == branch and branch in git.branches
    text = git.files(branch)[PATH][0]
    assert f"# qa-hub: {feature['code']}" in text
    assert git.branches[BRANCH] == source_head and git.files(BRANCH)[PATH][0] == FILE
    assert git.branches["main"] == main_head
    assert git.pulls == {} and "cleanup" not in feature["import_source"]
    status = await client.get(f"{BASE}/features/{feature['id']}/revisions/{rev['id']}/git-sync")
    assert status.json()["state"] == "in_sync"
    assert (await _rev(uuid.UUID(rev["id"]))).work_file_sha == blob_sha(text)


async def test_a_file_from_main_also_gets_a_branch_and_main_is_untouched(
    client, scope, actor, reviewer, git
):
    git.seed_file(PATH, FILE)
    feature = await _import(client, scope, actor, reviewer, url=URL.replace(BRANCH, "main"))
    branch = feature["working_revision"]["work_branch"]
    assert f"# qa-hub: {feature['code']}" in git.files(branch)[PATH][0]
    assert git.files("main")[PATH][0] == FILE


async def test_approval_opens_one_pr_from_the_working_branch_and_no_cleanup_pr(
    client, scope, actor, reviewer, git
):
    feature = await _import(client, scope, actor, reviewer)
    branch = feature["working_revision"]["work_branch"]
    out = (await _do(client, feature, "submit", actor["email"])).json()
    done = await _do(client, out, "approve", reviewer["email"])
    assert done.status_code == 200, done.text
    detail = await _get(client, feature["id"], actor["email"])
    assert detail["git_path"] == PATH
    assert len(git.pulls) == 1
    (pull,) = git.pulls.values()
    assert pull.head_branch == branch
    assert not [b for b in git.branches if "cleanup" in b]
    assert PATH in git.files(BRANCH) and git.files(BRANCH)[PATH][0] == FILE
    body = git.pull_meta[pull.number]["body"]
    assert BRANCH in body and "pulizia" not in body


async def test_the_import_succeeds_when_git_fails_at_the_branch(
    client, scope, actor, reviewer, git
):
    git.fail_once("put_file", 502, "GitHub giù")
    feature = await _import(client, scope, actor, reviewer)
    rev = feature["working_revision"]
    assert rev["work_branch"] == f"qa-hub/{feature['code']}-r1"
    status = await client.get(f"{BASE}/features/{feature['id']}/revisions/{rev['id']}/git-sync")
    assert status.json()["state"] == "file_missing"


async def test_the_cleanup_route_is_gone(client, scope, actor, reviewer, git):
    feature = await _import(client, scope, actor, reviewer)
    resp = await client.post(
        f"{BASE}/features/{feature['id']}/import-cleanup", json={"actor_email": actor["email"]}
    )
    assert resp.status_code in (404, 405)


async def test_import_does_not_overwrite_a_branch_that_already_holds_the_file(
    client, scope, actor, reviewer, git
):
    from sqlalchemy import update

    from app.models.test_lifecycle import TlFeatureRevision

    detail = await _ready_feature(client, scope, actor, reviewer)
    rev = detail["working_revision"]
    dev_branch, developer_text = "feature/dev", "# a developer's own version\n"
    git.branches[dev_branch] = git.branches["main"]
    git.seed_file(PATH, developer_text, branch=dev_branch)
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == uuid.UUID(rev["id"]))
            .values(work_branch=dev_branch)
        )
        await s.commit()
    resp = await client.post(
        f"{BASE}/features/{detail['id']}/revisions/{rev['id']}/import-git",
        json={"actor_email": actor["email"], "lock_version": rev["lock_version"], "url": URL},
    )
    assert resp.status_code == 200, resp.text
    assert git.files(dev_branch)[PATH][0] == developer_text  # untouched
    status = await client.get(f"{BASE}/features/{detail['id']}/revisions/{rev['id']}/git-sync")
    assert status.json()["state"] in ("repo_ahead", "diverged")

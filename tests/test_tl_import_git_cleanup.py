# ruff: noqa: S101, F811
"""Importa da Git, Task 3: percorso adottato e PR di pulizia sul branch di partenza."""

from __future__ import annotations

import pytest

from app.config import settings
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


async def _imported_and_approved(client, scope, actor, reviewer, url=URL):
    detail = await _ready_feature(client, scope, actor, reviewer)
    rev = detail["working_revision"]
    resp = await client.post(
        f"{BASE}/features/{detail['id']}/revisions/{rev['id']}/import-git",
        json={"actor_email": actor["email"], "lock_version": rev["lock_version"], "url": url},
    )
    assert resp.status_code == 200, resp.text
    out = (await _do(client, resp.json()["feature"], "submit", actor["email"])).json()
    done = await _do(client, out, "approve", reviewer["email"])
    assert done.status_code == 200, done.text
    return await _get(client, detail["id"], actor["email"])


async def test_approval_adopts_path_and_opens_cleanup_pr(client, scope, actor, reviewer, git):
    detail = await _imported_and_approved(client, scope, actor, reviewer)
    assert detail["git_path"] == PATH
    rev = detail["working_revision"] or detail["published_revision"]
    hub_branch = rev["pr_branch"] if "pr_branch" in rev else None
    hub_files = (
        git.files(hub_branch) if hub_branch else git.files(git.pulls[rev["pr_number"]].head_branch)
    )
    assert f"# qa-hub: {detail['code']}" in hub_files[PATH][0]
    body = git.pull_meta[rev["pr_number"]]["body"]
    assert BRANCH in body
    cleanup = detail["import_source"]["cleanup"]
    assert cleanup["state"] == "open" and cleanup["pr_number"] is not None
    meta = git.pull_meta[cleanup["pr_number"]]
    assert meta["base"] == BRANCH and detail["code"] in meta["title"]
    clean_branch = git.pulls[cleanup["pr_number"]].head_branch
    assert clean_branch == f"qa-hub/cleanup-{detail['code'].lower()}"
    assert PATH not in git.files(clean_branch) and PATH in git.files(BRANCH)


async def test_file_from_main_needs_no_cleanup(client, scope, actor, reviewer, git):
    git.seed_file(PATH, FILE)
    url = URL.replace(BRANCH, "main")
    detail = await _imported_and_approved(client, scope, actor, reviewer, url=url)
    assert detail["import_source"]["cleanup"]["state"] == "not_needed"
    assert len(git.pulls) == 1


async def test_deleted_branch_and_retry_after_error(client, scope, actor, reviewer, git):
    git.fail_once("delete_file", 502, "GitHub giù")
    detail = await _imported_and_approved(client, scope, actor, reviewer)
    cleanup = detail["import_source"]["cleanup"]
    assert cleanup["state"] == "error" and "GitHub giù" in cleanup["error"]
    assert detail["git_path"] == PATH  # la PR dell'hub è comunque aperta
    retry = await client.post(
        f"{BASE}/features/{detail['id']}/import-cleanup", json={"actor_email": actor["email"]}
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["import_source"]["cleanup"]["state"] == "open"


async def test_branch_gone_is_not_needed(client, scope, actor, reviewer, git, monkeypatch):
    from app.services.test_lifecycle import git_import_file

    detail = await _ready_feature(client, scope, actor, reviewer)
    rev = detail["working_revision"]
    await client.post(
        f"{BASE}/features/{detail['id']}/revisions/{rev['id']}/import-git",
        json={"actor_email": actor["email"], "lock_version": rev["lock_version"], "url": URL},
    )
    del git.branches[BRANCH]
    from tests._db import TestSession

    async with TestSession() as db:
        import uuid

        await git_import_file.open_cleanup_pr(db, uuid.UUID(detail["id"]), git, force=True)
    after = await _get(client, detail["id"], actor["email"])
    assert after["import_source"]["cleanup"]["state"] == "not_needed"
    assert "non esiste" in after["import_source"]["cleanup"]["note"]

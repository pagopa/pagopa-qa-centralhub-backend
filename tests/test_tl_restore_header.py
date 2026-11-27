# ruff: noqa: S101, F811
"""A managed feature file lost its `# qa-hub:` header: the hub asks and can restore it."""

from __future__ import annotations

import pytest

from app.config import settings
from app.models.test_lifecycle import TlIssue, TlIssueKind, TlIssueStatus
from app.services.test_lifecycle import git_import, restore_header
from app.services.test_lifecycle.feature_parser import parse_feature_file
from tests._db import TestSession
from tests.test_tl_features import BASE, _use_test_db, actor, scope  # noqa: F401
from tests.test_tl_git_import import (  # noqa: F401
    _import,
    _issues,
    _main_text,
    _merged,
)
from tests.test_tl_workflow import reviewer  # noqa: F401
from tests.tl_fakes import FakeGitClient, install_fake_git


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(settings, "tl_hub_base_url", "")
    monkeypatch.setattr(settings, "tl_base_branch", "main")
    return install_fake_git(monkeypatch, FakeGitClient())


def _without_header(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith("# qa-hub:")) + "\n"


async def _lost_header(client, scope, actor, reviewer, fake):
    """A merged feature whose file on main later lost the header."""
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    path = done["git_path"]
    edited = _without_header(_main_text(fake, done))
    sha = fake.seed_file(path, edited)
    await _import(fake, path, sha)
    return done, path, edited


async def _restore(issue, actor, fake):
    async with TestSession() as s:
        return await restore_header.restore_header(s, issue.id, actor["email"], git=fake)


async def test_losing_the_header_opens_a_question(client, scope, actor, reviewer, fake):
    done, path, _ = await _lost_header(client, scope, actor, reviewer, fake)
    issues = await _issues(done["id"], TlIssueKind.MISSING_HEADER)
    assert len(issues) == 1
    assert issues[0].status == TlIssueStatus.OPEN
    assert issues[0].path == path
    assert issues[0].detail["code"] == done["code"]


async def test_the_question_is_not_duplicated_by_a_new_sync(client, scope, actor, reviewer, fake):
    done, path, edited = await _lost_header(client, scope, actor, reviewer, fake)
    sha = fake.seed_file(path, edited + "\n")
    await _import(fake, path, sha)
    assert len(await _issues(done["id"], TlIssueKind.MISSING_HEADER)) == 1


async def test_restoring_opens_a_pr_that_only_adds_the_header(
    client, scope, actor, reviewer, fake
):
    done, path, edited = await _lost_header(client, scope, actor, reviewer, fake)
    issue = (await _issues(done["id"], TlIssueKind.MISSING_HEADER))[0]
    out = await _restore(issue, actor, fake)

    assert out.pr_url
    branch = next(p.head_branch for p in fake.pulls.values() if p.state == "open")
    proposed = fake.files(branch)[path][0]
    assert parse_feature_file(proposed).code == done["code"]
    assert proposed.endswith(edited)  # nothing else of the file changes
    assert fake.files("main")[path][0] == edited  # main is untouched until the merge


async def test_merging_the_restore_pr_resolves_the_question(client, scope, actor, reviewer, fake):
    done, path, _ = await _lost_header(client, scope, actor, reviewer, fake)
    issue = (await _issues(done["id"], TlIssueKind.MISSING_HEADER))[0]
    await _restore(issue, actor, fake)
    pull = next(p for p in fake.pulls.values() if p.state == "open")
    sha = fake.merge_pull(pull.number)
    await _import(fake, path, sha)
    issues = await _issues(done["id"], TlIssueKind.MISSING_HEADER)
    assert [i.status for i in issues] == [TlIssueStatus.RESOLVED]


async def test_restoring_twice_reuses_the_open_pr(client, scope, actor, reviewer, fake):
    done, _, _ = await _lost_header(client, scope, actor, reviewer, fake)
    issue = (await _issues(done["id"], TlIssueKind.MISSING_HEADER))[0]
    first = await _restore(issue, actor, fake)
    second = await _restore(issue, actor, fake)
    assert first.pr_url == second.pr_url
    restores = [c for c in fake.calls_of("create_pull") if c[1].startswith("qa-hub/restore-header")]
    assert len(restores) == 1


async def test_restoring_is_refused_when_the_file_has_another_features_header(
    client, scope, actor, reviewer, fake
):
    done, path, edited = await _lost_header(client, scope, actor, reviewer, fake)
    issue = (await _issues(done["id"], TlIssueKind.MISSING_HEADER))[0]
    fake.seed_file(path, "# qa-hub: FEAT_099_ALTRO\n" + edited)
    with pytest.raises(restore_header.RestoreConflict):
        await _restore(issue, actor, fake)


async def test_restoring_when_the_header_is_back_just_closes_the_question(
    client, scope, actor, reviewer, fake
):
    done, path, edited = await _lost_header(client, scope, actor, reviewer, fake)
    issue = (await _issues(done["id"], TlIssueKind.MISSING_HEADER))[0]
    fake.seed_file(path, f"# qa-hub: {done['code']}\n" + edited)
    out = await _restore(issue, actor, fake)
    assert out.pr_url is None
    async with TestSession() as s:
        assert (await s.get(TlIssue, issue.id)).status == TlIssueStatus.RESOLVED


async def test_other_questions_cannot_be_restored(client, scope, actor, reviewer, fake):
    done, _, _ = await _lost_header(client, scope, actor, reviewer, fake)
    issue = (await _issues(done["id"], TlIssueKind.MISSING_HEADER))[0]
    async with TestSession() as s:
        row = await s.get(TlIssue, issue.id)
        row.kind = TlIssueKind.PARSE_ERROR
        await s.commit()
    with pytest.raises(restore_header.RestoreNotApplicable):
        await _restore(issue, actor, fake)


async def test_api_restores_the_header(client, scope, actor, reviewer, fake):
    done, _, _ = await _lost_header(client, scope, actor, reviewer, fake)
    issue = (await _issues(done["id"], TlIssueKind.MISSING_HEADER))[0]
    resp = await client.post(
        f"{BASE}/issues/{issue.id}/restore-header", json={"actor_email": actor["email"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pr_url"]


async def test_headerless_new_file_still_goes_through_the_import_not_this(
    client, scope, actor, fake
):
    # a file no feature manages is an `unmanaged_file`, never a `missing_header`
    path = "features/ignored/x.feature"
    sha = fake.seed_file(path, "Feature: x\n  Scenario: y\n    Given z\n")
    async with TestSession() as s:
        await git_import.import_changed_file(s, path, None, sha, fake)
    assert await _issues(kind=TlIssueKind.MISSING_HEADER, path=path) == []

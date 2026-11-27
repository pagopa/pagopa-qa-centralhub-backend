# ruff: noqa: S101, F811
"""Adopting a feature file the hub did not manage (design 2026-11-27)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, func, select, update

from app.config import settings
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureStatus,
    TlIssue,
    TlIssueKind,
    TlIssueStatus,
    TlMigrationScope,
    TlScenario,
    TlScope,
)
from app.services.test_lifecycle import adopt, git_import, migration_align
from app.services.test_lifecycle.feature_parser import parse_feature_file
from tests._db import TestSession
from tests.test_tl_features import BASE, _use_test_db, actor, scope  # noqa: F401
from tests.tl_fakes import FakeGitClient, install_fake_git

HEADERLESS = """# language: it
@integration
Funzionalità: Pagamento

  @legacy_tag @positivo
  Scenario: Paga
    Dato un sistema
    Quando pago
    Allora funziona

  @negativo
  Scenario: Rifiuta
    Dato un sistema
    Quando pago male
    Allora fallisce
"""


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(settings, "tl_hub_base_url", "")
    monkeypatch.setattr(settings, "tl_base_branch", "main")
    return install_fake_git(monkeypatch, FakeGitClient())


@pytest.fixture
async def folder(scope):
    """The scope owns a folder no other scope of the shared test database uses."""
    value = f"features/{uuid.uuid4().hex[:10]}"
    async with TestSession() as s:
        await s.execute(
            update(TlScope).where(TlScope.id == uuid.UUID(scope["id"])).values(repo_folder=value)
        )
        await s.commit()
    yield value
    async with TestSession() as s:
        await s.execute(
            delete(TlMigrationScope).where(TlMigrationScope.scope_code == scope["code"])
        )
        await s.commit()


async def _unmanaged_issue(fake, path: str, text: str) -> TlIssue:
    sha = fake.seed_file(path, text)
    async with TestSession() as s:
        await git_import.import_changed_file(s, path, None, sha, fake)
    async with TestSession() as s:
        return (
            await s.execute(select(TlIssue).where(TlIssue.path == path))
        ).scalars().one()


async def _adopt(issue, actor, fake, **kwargs):
    async with TestSession() as s:
        return await adopt.adopt_file(s, issue.id, actor["email"], git=fake, **kwargs)


async def _feature_by_code(code: str) -> TlFeature:
    async with TestSession() as s:
        return (await s.execute(select(TlFeature).where(TlFeature.code == code))).scalar_one()


async def test_adopt_headerless_file_creates_a_published_feature(scope, folder, actor, fake):
    path = f"{folder}/pagamento.feature"
    issue = await _unmanaged_issue(fake, path, HEADERLESS)
    out = await _adopt(issue, actor, fake)

    code = f"FEAT_001_{scope['code']}"
    assert out.code == code
    feature = await _feature_by_code(code)
    assert feature.status == TlFeatureStatus.IMPLEMENTED
    assert feature.git_path == path
    assert feature.published_revision_id is not None
    async with TestSession() as s:
        tags = list(
            (
                await s.execute(
                    select(TlScenario.tag)
                    .where(TlScenario.feature_id == feature.id)
                    .order_by(TlScenario.number)
                )
            ).scalars()
        )
    prefix = scope["code"].upper()
    assert tags == [f"@{prefix}_001_01", f"@{prefix}_001_02"]


async def test_adopting_resolves_the_issue(scope, folder, actor, fake):
    issue = await _unmanaged_issue(fake, f"{folder}/a.feature", HEADERLESS)
    await _adopt(issue, actor, fake)
    async with TestSession() as s:
        assert (await s.get(TlIssue, issue.id)).status == TlIssueStatus.RESOLVED


async def test_headerless_adoption_opens_an_alignment_pr_with_the_header(
    scope, folder, actor, fake
):
    path = f"{folder}/a.feature"
    issue = await _unmanaged_issue(fake, path, HEADERLESS)
    out = await _adopt(issue, actor, fake)

    assert out.alignment_pending is True
    assert out.pr_url
    branch = migration_align.branch_for(scope["code"])
    aligned = fake.files(branch)[path][0]
    assert parse_feature_file(aligned).code == out.code
    assert "@legacy_tag" in aligned  # category tags are kept
    # the file on main is untouched until the pull request is merged
    assert fake.files("main")[path][0] == HEADERLESS
    feature = await _feature_by_code(out.code)
    assert feature.alignment_pending is True


async def test_merging_the_alignment_pr_closes_the_adoption(scope, folder, actor, fake):
    path = f"{folder}/a.feature"
    issue = await _unmanaged_issue(fake, path, HEADERLESS)
    out = await _adopt(issue, actor, fake)
    pull = next(p for p in fake.pulls.values() if p.state == "open")
    fake.merge_pull(pull.number)
    async with TestSession() as s:
        await migration_align.sync_alignment_prs(s, fake)
    assert (await _feature_by_code(out.code)).alignment_pending is False


async def test_adoption_needs_the_scope_when_the_folder_is_ambiguous(scope, folder, actor, fake):
    other_code = "T" + uuid.uuid4().hex[:8]
    async with TestSession() as s:
        other = TlScope(code=other_code, name="Altro", repo_folder=folder, next_feature_number=1)
        s.add(other)
        await s.commit()
        other_id = other.id
    try:
        issue = await _unmanaged_issue(fake, f"{folder}/a.feature", HEADERLESS)
        with pytest.raises(adopt.AdoptScopeRequired):
            await _adopt(issue, actor, fake)
        out = await _adopt(issue, actor, fake, scope_code=scope["code"])
        assert out.code == f"FEAT_001_{scope['code']}"
    finally:
        async with TestSession() as s:
            await s.execute(delete(TlScope).where(TlScope.id == other_id))
            await s.commit()


async def test_file_with_a_valid_header_keeps_its_code(scope, folder, actor, fake):
    code = f"FEAT_007_{scope['code']}"
    text = f"# qa-hub: {code}\n" + HEADERLESS
    issue = await _unmanaged_issue_with_header(fake, f"{folder}/h.feature", text)
    out = await _adopt(issue, actor, fake)
    assert out.code == code
    async with TestSession() as s:
        stored = (
            await s.execute(select(TlScope).where(TlScope.code == scope["code"]))
        ).scalar_one()
    assert stored.next_feature_number == 8  # the counter moves past the adopted number


async def _unmanaged_issue_with_header(fake, path: str, text: str) -> TlIssue:
    """A file whose header names a code the hub does not know: `unknown_feature_code`."""
    sha = fake.seed_file(path, text)
    async with TestSession() as s:
        await git_import.import_changed_file(s, path, None, sha, fake)
    async with TestSession() as s:
        issue = (await s.execute(select(TlIssue).where(TlIssue.path == path))).scalars().one()
    assert issue.kind == TlIssueKind.UNKNOWN_FEATURE_CODE
    return issue


async def test_second_file_with_an_adopted_code_is_not_a_new_feature(scope, folder, actor, fake):
    first = await _unmanaged_issue(fake, f"{folder}/a.feature", HEADERLESS)
    out = await _adopt(first, actor, fake)
    text = f"# qa-hub: {out.code}\n" + HEADERLESS
    # same code on another path: the hub reports the duplicate instead of adopting twice
    sha = fake.seed_file(f"{folder}/b.feature", text)
    async with TestSession() as s:
        await git_import.import_changed_file(s, f"{folder}/b.feature", None, sha, fake)
    async with TestSession() as s:
        count = (
            await s.execute(
                select(func.count()).select_from(TlFeature).where(TlFeature.code == out.code)
            )
        ).scalar_one()
    assert count == 1


async def test_adoption_fails_when_the_file_is_gone(scope, folder, actor, fake):
    path = f"{folder}/a.feature"
    issue = await _unmanaged_issue(fake, path, HEADERLESS)
    fake.remove_file(path)
    with pytest.raises(adopt.AdoptFileMissing):
        await _adopt(issue, actor, fake)


async def test_adoption_refuses_a_file_that_does_not_parse(scope, folder, actor, fake):
    path = f"{folder}/a.feature"
    issue = await _unmanaged_issue(fake, path, HEADERLESS)
    fake.seed_file(path, HEADERLESS + "\n  Regola: non supportata\n")
    with pytest.raises(adopt.AdoptParseError):
        await _adopt(issue, actor, fake)


async def test_only_import_questions_can_be_adopted(scope, folder, actor, fake):
    issue = await _unmanaged_issue(fake, f"{folder}/a.feature", HEADERLESS)
    async with TestSession() as s:
        row = await s.get(TlIssue, issue.id)
        row.kind = TlIssueKind.PARSE_ERROR
        await s.commit()
    with pytest.raises(adopt.AdoptNotApplicable):
        await _adopt(issue, actor, fake)


async def test_a_resolved_question_cannot_be_adopted_again(scope, folder, actor, fake):
    issue = await _unmanaged_issue(fake, f"{folder}/a.feature", HEADERLESS)
    await _adopt(issue, actor, fake)
    with pytest.raises(adopt.AdoptNotApplicable):
        await _adopt(issue, actor, fake)


async def test_api_imports_the_file_of_an_issue(client, scope, folder, actor, fake):
    issue = await _unmanaged_issue(fake, f"{folder}/a.feature", HEADERLESS)
    resp = await client.post(
        f"{BASE}/issues/{issue.id}/import", json={"actor_email": actor["email"]}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == f"FEAT_001_{scope['code']}"
    assert body["alignment_pending"] is True and body["pr_url"]

    again = await client.post(
        f"{BASE}/issues/{issue.id}/import", json={"actor_email": actor["email"]}
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "adopt_not_applicable"


async def test_api_asks_for_the_scope_when_it_is_unknown(client, scope, folder, actor, fake):
    issue = await _unmanaged_issue(fake, f"{folder}/a.feature", HEADERLESS)
    async with TestSession() as s:
        row = await s.get(TlIssue, issue.id)
        row.detail = {**row.detail, "scope_code": None}
        await s.commit()
    resp = await client.post(
        f"{BASE}/issues/{issue.id}/import", json={"actor_email": actor["email"]}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "adopt_scope_required"
    ok = await client.post(
        f"{BASE}/issues/{issue.id}/import",
        json={"actor_email": actor["email"], "scope_code": scope["code"]},
    )
    assert ok.status_code == 200, ok.text

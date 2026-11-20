# ruff: noqa: F811, S101
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import delete, func, select

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureVersion,
    TlIssue,
    TlIssueKind,
    TlIssueStatus,
    TlRevisionStatus,
    TlScenario,
    TlScenarioContent,
)
from app.services.test_lifecycle import git_import
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _use_test_db,
    actor,
    scope,
)
from tests.test_tl_git_publish import _approved
from tests.test_tl_workflow import reviewer  # noqa: F401
from tests.tl_fakes import FakeGitClient, install_fake_git

PR = 101


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(settings, "tl_hub_base_url", "")
    monkeypatch.setattr(settings, "tl_base_branch", "main")
    return install_fake_git(monkeypatch, FakeGitClient())


# ── helpers ───────────────────────────────────────────────────────────────────

async def _feature(fid) -> TlFeature:
    async with TestSession() as s:
        return await s.get(TlFeature, uuid.UUID(str(fid)))


async def _rev(rid) -> TlFeatureRevision:
    async with TestSession() as s:
        return await s.get(TlFeatureRevision, uuid.UUID(str(rid)))


async def _versions(fid) -> list[TlFeatureVersion]:
    async with TestSession() as s:
        return list(
            (
                await s.execute(
                    select(TlFeatureVersion)
                    .where(TlFeatureVersion.feature_id == uuid.UUID(str(fid)))
                    .order_by(TlFeatureVersion.created_at)
                )
            ).scalars()
        )


async def _issues(fid=None, kind=None, path=None) -> list[TlIssue]:
    stmt = select(TlIssue)
    if fid is not None:
        stmt = stmt.where(TlIssue.feature_id == uuid.UUID(str(fid)))
    if kind is not None:
        stmt = stmt.where(TlIssue.kind == kind)
    if path is not None:
        stmt = stmt.where(TlIssue.path == path)
    async with TestSession() as s:
        return list((await s.execute(stmt.order_by(TlIssue.created_at))).scalars())


async def _events(fid, action) -> list[dict]:
    async with TestSession() as s:
        rows = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.resource_type == "tl_feature",
                    AuditLog.resource_id == str(fid),
                    AuditLog.action == action,
                )
            )
        ).scalars()
        return [json.loads(r.detail) for r in rows]


async def _detail(client, fid):
    resp = await client.get(f"{BASE}/features/{fid}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _merge(done, fake, edit=None) -> str:
    """Merge della PR della revisione approvata, con modifica opzionale del file sul branch."""
    path = done["git_path"]
    branch = f"qa-hub/{done['code']}-r1"
    if edit is not None:
        fake.seed_file(path, edit(fake.files(branch)[path][0]), branch)
    sha = fake.merge_pull(PR)
    async with TestSession() as s:
        rev = await s.get(TlFeatureRevision, uuid.UUID(done["working_revision"]["id"]))
        await git_import.mark_revision_merged(s, rev, sha, fake)
    return sha


async def _import(fake, path, sha, previous_path=None):
    async with TestSession() as s:
        await git_import.import_changed_file(s, path, previous_path, sha, fake)


async def _merged(client, scope, actor, reviewer, fake, edit=None):
    done = await _approved(client, scope, actor, reviewer)
    sha = await _merge(done, fake, edit)
    return done, sha


def _main_text(fake, done) -> str:
    return fake.files("main")[done["git_path"]][0]


def _tags(scope, number=1):
    prefix = scope["code"].upper()
    return f"@{prefix}_{number:03d}", lambda n: f"@{prefix}_{number:03d}_{n:02d}"


EXTRA_SCENARIO = """
  @negativo
  {tag}
  Scenario: Pagamento rifiutato
    Dato un utente registrato
    Quando paga con una carta scaduta
    Allora il pagamento è rifiutato
"""


async def _working_revision(feature_id, base_id, status, **extra) -> uuid.UUID:
    """Revisione di lavoro aperta sulla pubblicata (la riapertura arriva con il Task 9)."""
    async with TestSession() as s:
        feature = await s.get(TlFeature, uuid.UUID(str(feature_id)))
        rev = TlFeatureRevision(
            feature_id=feature.id,
            number=2,
            status=status,
            base_revision_id=uuid.UUID(str(base_id)),
            title="Pagamento",
            created_by="someone@example.test",
            **extra,
        )
        s.add(rev)
        await s.flush()
        feature.working_revision_id = rev.id
        await s.commit()
        return rev.id


# ── merge ─────────────────────────────────────────────────────────────────────

async def test_merge_publishes_and_sets_implemented(client, scope, actor, reviewer, fake):
    done, sha = await _merged(client, scope, actor, reviewer, fake)
    rev_id = done["working_revision"]["id"]
    detail = await _detail(client, done["id"])
    assert detail["status"] == "implemented"
    assert detail["working_revision"] is None
    pub = detail["published_revision"]
    assert pub["id"] == rev_id
    assert pub["status"] == "merged"
    assert pub["pr_state"] == "merged"
    assert pub["merged_commit_sha"] == sha
    assert detail["open_issues"] == 0
    # contenuto identico al render approvato: nessuna versione git
    assert [v.origin for v in await _versions(done["id"])] == ["hub"]
    merged = await _events(done["id"], "tl.pr.merged")
    assert len(merged) == 1
    assert merged[0]["merged_commit_sha"] == sha
    assert await _events(done["id"], "tl.git.imported") == []

    # idempotente: un secondo merge della stessa revisione non fa nulla
    async with TestSession() as s:
        rev = await s.get(TlFeatureRevision, uuid.UUID(rev_id))
        await git_import.mark_revision_merged(s, rev, sha, fake)
    assert len(await _events(done["id"], "tl.pr.merged")) == 1


async def test_merged_content_differs_from_approved_render(client, scope, actor, reviewer, fake):
    def edit(text):
        return text.replace("Quando paga\n", "Quando paga con la carta\n").replace(
            "Funzionalità: Pagamento", "Funzionalità: Pagamento online"
        )

    done, sha = await _merged(client, scope, actor, reviewer, fake, edit)
    rev_id = done["working_revision"]["id"]
    detail = await _detail(client, done["id"])
    pub = detail["published_revision"]
    assert pub["id"] == rev_id  # la revisione mergiata, aggiornata: nessuna revisione nuova
    assert pub["title"] == "Pagamento online"
    assert "Quando paga con la carta" in pub["scenarios"][0]["steps"]
    assert detail["status"] == "implemented"  # solo differenze testuali
    assert detail["open_issues"] == 0
    versions = await _versions(done["id"])
    assert [v.origin for v in versions] == ["hub", "git"]
    assert versions[-1].commit_sha == sha
    assert versions[-1].revision_id == uuid.UUID(rev_id)
    assert versions[-1].actor == git_import.SYSTEM_ACTOR
    assert versions[-1].snapshot["file"] == _main_text(fake, done)
    imported = await _events(done["id"], "tl.git.imported")
    assert len(imported) == 1
    assert imported[0]["commit_sha"] == sha
    async with TestSession() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(TlFeatureRevision)
                .where(TlFeatureRevision.feature_id == uuid.UUID(done["id"]))
            )
        ).scalar_one()
    assert n == 1

    # il sync reimporta lo stesso file da main: nessuna versione nuova
    await _import(fake, done["git_path"], sha)
    assert len(await _versions(done["id"])) == 2


async def test_structural_change_opens_issue_and_keeps_designed(
    client, scope, actor, reviewer, fake
):
    _, tag = _tags(scope)

    def edit(text):
        # identità rimossa (scenario senza tag identità): strutturale
        return text.replace(f"  {tag(1)}\n", "")

    done, sha = await _merged(client, scope, actor, reviewer, fake, edit)
    detail = await _detail(client, done["id"])
    assert detail["status"] == "designed"
    assert detail["published_revision"]["id"] == done["working_revision"]["id"]
    assert detail["published_revision"]["pr_state"] == "merged"
    issues = await _issues(done["id"], TlIssueKind.STRUCTURAL_CHANGE)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.status == TlIssueStatus.OPEN
    assert issue.commit_sha == sha
    assert issue.path == done["git_path"]
    kinds = {c["kind"] for c in issue.detail["changes"]}
    assert {"missing_tag", "removed_scenario"} <= kinds
    # Git è la verità: lo scenario senza identità non è più nella pubblicata
    assert detail["published_revision"]["scenarios"] == []

    # un reimport dello stesso file non duplica l'anomalia
    await _import(fake, done["git_path"], sha)
    assert len(await _issues(done["id"], TlIssueKind.STRUCTURAL_CHANGE)) == 1


# ── import da main ────────────────────────────────────────────────────────────

async def test_git_edit_creates_git_version_textual_no_issue(
    client, scope, actor, reviewer, fake
):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    path = done["git_path"]
    r1 = done["working_revision"]["id"]
    text = _main_text(fake, done).replace("Quando paga\n", "Quando paga in contanti\n")
    sha = fake.seed_file(path, text)
    await _import(fake, path, sha)

    detail = await _detail(client, done["id"])
    pub = detail["published_revision"]
    assert pub["id"] != r1
    assert pub["number"] == 2
    assert pub["origin"] == "git"
    assert pub["status"] == "merged"
    assert pub["merged_commit_sha"] == sha
    assert "Quando paga in contanti" in pub["scenarios"][0]["steps"]
    assert pub["scenarios"][0]["tag"] == done["working_revision"]["scenarios"][0]["tag"]
    assert (await _rev(r1)).status == TlRevisionStatus.SUPERSEDED
    assert (await _rev(pub["id"])).base_revision_id == uuid.UUID(r1)
    assert detail["status"] == "implemented"
    assert detail["open_issues"] == 0
    assert await _issues(done["id"]) == []
    versions = await _versions(done["id"])
    assert [v.origin for v in versions] == ["hub", "git"]
    assert versions[-1].commit_sha == sha
    assert versions[-1].revision_id == uuid.UUID(pub["id"])
    imported = await _events(done["id"], "tl.git.imported")
    assert len(imported) == 1
    assert imported[0]["revision_id"] == pub["id"]
    assert imported[0]["textual"]


async def test_new_valid_scenario_creates_identity_and_issue(
    client, scope, actor, reviewer, fake
):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    path = done["git_path"]
    _, tag = _tags(scope)
    text = _main_text(fake, done) + EXTRA_SCENARIO.format(tag=tag(5))
    sha = fake.seed_file(path, text)
    await _import(fake, path, sha)

    detail = await _detail(client, done["id"])
    pub = detail["published_revision"]
    assert [s["tag"] for s in pub["scenarios"]] == [tag(1), tag(5)]
    new = pub["scenarios"][1]
    assert new["number"] == 5
    assert new["polarity"] == "negativo"
    assert new["title"] == "Pagamento rifiutato"
    assert "carta scaduta" in new["steps"]
    feature = await _feature(done["id"])
    assert feature.next_scenario_number == 6
    issues = await _issues(done["id"], TlIssueKind.NEW_SCENARIO_FROM_GIT)
    assert len(issues) == 1
    assert issues[0].detail["tag"] == tag(5)
    assert issues[0].revision_id == uuid.UUID(pub["id"])
    assert await _issues(done["id"], TlIssueKind.STRUCTURAL_CHANGE) == []
    assert detail["status"] == "implemented"

    # reimport: identità già nota, nessuna nuova anomalia né versione
    n_versions = len(await _versions(done["id"]))
    await _import(fake, path, sha)
    assert len(await _versions(done["id"])) == n_versions
    assert len(await _issues(done["id"], TlIssueKind.NEW_SCENARIO_FROM_GIT)) == 1
    async with TestSession() as s:
        count = (
            await s.execute(
                select(func.count())
                .select_from(TlScenario)
                .where(TlScenario.feature_id == uuid.UUID(done["id"]))
            )
        ).scalar_one()
    assert count == 2


async def test_reimport_identical_content_creates_no_version(
    client, scope, actor, reviewer, fake
):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    path = done["git_path"]
    original = _main_text(fake, done)
    noisy = "﻿" + "\r\n".join(line + "   " for line in original.split("\n")) + "\r\n\r\n"
    sha = fake.seed_file(path, noisy)
    await _import(fake, path, sha)
    await _import(fake, path, sha)

    detail = await _detail(client, done["id"])
    assert detail["published_revision"]["id"] == done["working_revision"]["id"]
    assert [v.origin for v in await _versions(done["id"])] == ["hub"]
    assert await _issues(done["id"]) == []
    assert await _events(done["id"], "tl.git.imported") == []

    # differenze solo cosmetiche (spazi negli step, allineamento): nessuna versione
    cosmetic = original.replace("    Quando paga\n", "        Quando paga\n")
    sha = fake.seed_file(path, cosmetic)
    await _import(fake, path, sha)
    assert [v.origin for v in await _versions(done["id"])] == ["hub"]
    assert await _issues(done["id"]) == []


async def test_moved_file_updates_git_path_without_issue(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    old = done["git_path"]
    new = f"features/altro/{done['code']}_spostato.feature"
    text = _main_text(fake, done)
    fake.seed_file(new, text)
    sha = fake.remove_file(old)
    # compare senza rinomina riconosciuta: added + removed, prima i file presenti
    await _import(fake, new, sha)
    async with TestSession() as s:
        await git_import.handle_removed_file(s, old, sha)

    detail = await _detail(client, done["id"])
    assert detail["git_path"] == new
    assert detail["open_issues"] == 0
    assert await _issues(done["id"]) == []
    assert [v.origin for v in await _versions(done["id"])] == ["hub"]

    # rinomina riconosciuta da GitHub (previous_path)
    newer = f"features/altro/{done['code']}_rinominato.feature"
    fake.seed_file(newer, text)
    sha = fake.remove_file(new)
    await _import(fake, newer, sha, previous_path=new)
    assert (await _detail(client, done["id"]))["git_path"] == newer
    assert await _issues(done["id"]) == []


async def test_copied_file_does_not_move_git_path(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    old = done["git_path"]
    copy = f"features/altro/{done['code']}_copia.feature"
    sha = fake.seed_file(copy, _main_text(fake, done))
    await _import(fake, copy, sha)
    assert (await _detail(client, done["id"]))["git_path"] == old
    issues = await _issues(done["id"], TlIssueKind.STRUCTURAL_CHANGE)
    assert len(issues) == 1
    assert issues[0].path == copy
    assert issues[0].detail["changes"][0]["kind"] == "duplicate_file"


async def test_removed_managed_file_opens_issue(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    path = done["git_path"]
    sha = fake.remove_file(path)
    async with TestSession() as s:
        await git_import.handle_removed_file(s, path, sha)
    issues = await _issues(done["id"], TlIssueKind.FILE_DELETED)
    assert len(issues) == 1
    assert issues[0].status == TlIssueStatus.OPEN
    assert issues[0].path == path
    assert issues[0].commit_sha == sha

    # il file ricompare: l'anomalia si risolve da sola
    sha = fake.seed_file(path, _main_text_from_version(await _versions(done["id"])))
    await _import(fake, path, sha)
    issues = await _issues(done["id"], TlIssueKind.FILE_DELETED)
    assert [i.status for i in issues] == [TlIssueStatus.RESOLVED]


def _main_text_from_version(versions):
    return versions[-1].snapshot["file"]


async def test_removed_unmanaged_file_is_ignored(fake):
    async with TestSession() as s:
        await git_import.handle_removed_file(s, f"x/{uuid.uuid4().hex}.feature", "abc")
    # nessuna eccezione, nessuna anomalia senza feature per quel percorso


async def test_unknown_code_opens_issue(fake):
    path = f"features/ignoto/{uuid.uuid4().hex}.feature"
    code = f"FEAT_999_Z{uuid.uuid4().hex[:6]}"
    text = f"# qa-hub: {code}\n# language: it\nFunzionalità: Boh\n"
    sha = fake.seed_file(path, text)
    try:
        await _import(fake, path, sha)
        await _import(fake, path, sha)
        issues = await _issues(path=path)
        assert len(issues) == 1
        assert issues[0].kind == TlIssueKind.UNKNOWN_FEATURE_CODE
        assert issues[0].feature_id is None
        assert issues[0].detail["code"] == code
    finally:
        async with TestSession() as s:
            await s.execute(delete(TlIssue).where(TlIssue.path == path))
            await s.commit()


async def test_unmanaged_file_without_header_is_ignored(fake):
    path = f"features/legacy/{uuid.uuid4().hex}.feature"
    sha = fake.seed_file(path, "#123456\nFeature: legacy\n  Scenario: x\n    Given y\n")
    await _import(fake, path, sha)
    assert await _issues(path=path) == []


async def test_parse_error_opens_issue(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    path = done["git_path"]
    broken = _main_text(fake, done) + "\n  Regola: non supportata\n"
    sha = fake.seed_file(path, broken)
    await _import(fake, path, sha)
    issues = await _issues(done["id"], TlIssueKind.PARSE_ERROR)
    assert len(issues) == 1
    assert issues[0].detail["errors"]
    assert [v.origin for v in await _versions(done["id"])] == ["hub"]


# ── revisione di lavoro ───────────────────────────────────────────────────────

async def test_open_working_revision_becomes_stale(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    r1 = done["working_revision"]["id"]
    r2 = await _working_revision(done["id"], r1, TlRevisionStatus.DRAFT)
    path = done["git_path"]
    sha = fake.seed_file(path, _main_text(fake, done).replace("Quando paga\n", "Quando paga!\n"))
    await _import(fake, path, sha)

    rev = await _rev(r2)
    assert rev.base_stale is True
    assert rev.status == TlRevisionStatus.DRAFT
    feature = await _feature(done["id"])
    assert feature.working_revision_id == r2
    assert feature.published_revision_id not in (uuid.UUID(r1), r2)
    assert len(await _events(done["id"], "tl.revision.stale")) == 1


async def test_approved_revision_returns_to_draft_when_stale(
    client, scope, actor, reviewer, fake
):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    r1 = done["working_revision"]["id"]
    pull = await fake.create_pull(f"qa-hub/{done['code']}-r2", "main", "r2", "")
    r2 = await _working_revision(
        done["id"], r1, TlRevisionStatus.APPROVED, pr_state="open", pr_number=pull.number
    )
    path = done["git_path"]
    sha = fake.seed_file(path, _main_text(fake, done).replace("Quando paga\n", "Quando paga!\n"))
    await _import(fake, path, sha)

    rev = await _rev(r2)
    assert rev.base_stale is True
    assert rev.status == TlRevisionStatus.DRAFT
    assert rev.pr_state == "open"  # la PR resta aperta, aggiornata dopo il riallineamento
    assert rev.pr_number == pull.number
    stale = await _events(done["id"], "tl.revision.stale")
    assert len(stale) == 1
    assert stale[0]["from"] == "approved"
    assert stale[0]["to"] == "draft"


async def test_scenario_contents_follow_git_order(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    path = done["git_path"]
    _, tag = _tags(scope)
    text = _main_text(fake, done) + EXTRA_SCENARIO.format(tag=tag(2))
    sha = fake.seed_file(path, text)
    await _import(fake, path, sha)
    pub_id = (await _feature(done["id"])).published_revision_id
    async with TestSession() as s:
        rows = (
            await s.execute(
                select(TlScenarioContent.position)
                .where(TlScenarioContent.revision_id == pub_id)
                .order_by(TlScenarioContent.position)
            )
        ).scalars().all()
    assert rows == [1, 2]


async def test_header_removed_on_managed_path_is_structural(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    path = done["git_path"]
    text = _main_text(fake, done).replace(f"# qa-hub: {done['code']}\n", "")
    sha = fake.seed_file(path, text)
    await _import(fake, path, sha)
    issues = await _issues(done["id"], TlIssueKind.STRUCTURAL_CHANGE)
    assert len(issues) == 1
    assert [c["kind"] for c in issues[0].detail["changes"]] == ["header_missing"]
    # contenuto invariato: nessuna versione
    assert [v.origin for v in await _versions(done["id"])] == ["hub"]


# ── fix round 1 ───────────────────────────────────────────────────────────────

async def test_parse_error_at_merge_keeps_designed(client, scope, actor, reviewer, fake):
    original = {}

    def edit(text):
        original["text"] = text
        return text + "\n  Regola: non supportata\n"

    done, _ = await _merged(client, scope, actor, reviewer, fake, edit)
    detail = await _detail(client, done["id"])
    assert detail["status"] == "designed"
    assert detail["published_revision"]["pr_state"] == "merged"
    parse = await _issues(done["id"], TlIssueKind.PARSE_ERROR)
    assert [i.status for i in parse] == [TlIssueStatus.OPEN]

    # Git corretto più tardi: l'anomalia si risolve e la feature diventa implemented
    path = done["git_path"]
    sha = fake.seed_file(path, original["text"])
    await _import(fake, path, sha)
    parse = await _issues(done["id"], TlIssueKind.PARSE_ERROR)
    assert [i.status for i in parse] == [TlIssueStatus.RESOLVED]
    assert (await _detail(client, done["id"]))["status"] == "implemented"
    assert len(await _events(done["id"], "tl.feature.implemented")) == 1


async def test_removed_before_moved_import_opens_nothing(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    old = done["git_path"]
    new = f"features/altro/{done['code']}_spostato.feature"
    fake.seed_file(new, _main_text(fake, done))
    sha = fake.remove_file(old)
    # ordine inverso: prima la rimozione, con il contesto del compare
    async with TestSession() as s:
        await git_import.handle_removed_file(s, old, sha, fake, present_paths=[new])
    assert await _issues(done["id"]) == []
    await _import(fake, new, sha)
    assert (await _detail(client, done["id"]))["git_path"] == new
    assert await _issues(done["id"]) == []


async def test_import_before_mark_merged_creates_no_extra_revision(
    client, scope, actor, reviewer, fake
):
    done = await _approved(client, scope, actor, reviewer)
    r1 = done["working_revision"]["id"]
    sha = fake.merge_pull(PR)
    # ordine sbagliato: il compare importa il file prima della registrazione del merge
    await _import(fake, done["git_path"], sha)
    detail = await _detail(client, done["id"])
    assert detail["published_revision"]["id"] == r1
    assert detail["published_revision"]["pr_state"] == "merged"
    assert detail["working_revision"] is None
    assert detail["status"] == "implemented"
    assert await _events(done["id"], "tl.revision.stale") == []
    assert [v.origin for v in await _versions(done["id"])] == ["hub"]
    async with TestSession() as s:
        rev = await s.get(TlFeatureRevision, uuid.UUID(r1))
        await git_import.mark_revision_merged(s, rev, sha, fake)
        n = (
            await s.execute(
                select(func.count())
                .select_from(TlFeatureRevision)
                .where(TlFeatureRevision.feature_id == uuid.UUID(done["id"]))
            )
        ).scalar_one()
    assert n == 1
    assert len(await _events(done["id"], "tl.pr.merged")) == 1


async def test_known_identity_is_not_a_new_scenario(client, scope, actor, reviewer, fake):
    from app.services.test_lifecycle import history
    from app.services.test_lifecycle.gherkin import render_feature_file
    from tests.test_tl_workflow import _ready_feature

    detail = await _ready_feature(client, scope, actor, reviewer)  # bozza, identità 01 esiste
    async with TestSession() as s:
        feature = await s.get(TlFeature, uuid.UUID(detail["id"]))
        rev = await s.get(TlFeatureRevision, uuid.UUID(detail["working_revision"]["id"]))
        render, _ = await history.build_render(s, feature, rev)
    path = f"features/ambito/{detail['code']}_diretto.feature"
    sha = fake.seed_file(path, render_feature_file(render))  # push diretto su main
    await _import(fake, path, sha)
    out = await _detail(client, detail["id"])
    assert [sc["tag"] for sc in out["published_revision"]["scenarios"]] == [
        sc["tag"] for sc in detail["working_revision"]["scenarios"]
    ]
    assert await _issues(detail["id"], TlIssueKind.NEW_SCENARIO_FROM_GIT) == []


async def test_acknowledged_structural_not_reopened(client, scope, actor, reviewer, fake):
    _, tag = _tags(scope)
    done, sha = await _merged(
        client, scope, actor, reviewer, fake, lambda t: t.replace(f"  {tag(1)}\n", "")
    )
    async with TestSession() as s:
        issue = (
            await s.execute(
                select(TlIssue).where(
                    TlIssue.feature_id == uuid.UUID(done["id"]),
                    TlIssue.kind == TlIssueKind.STRUCTURAL_CHANGE,
                )
            )
        ).scalar_one()
        issue.status = TlIssueStatus.ACKNOWLEDGED
        await s.commit()
    await _import(fake, done["git_path"], sha)
    issues = await _issues(done["id"], TlIssueKind.STRUCTURAL_CHANGE)
    assert [i.status for i in issues] == [TlIssueStatus.ACKNOWLEDGED]
    assert (await _detail(client, done["id"]))["status"] == "designed"

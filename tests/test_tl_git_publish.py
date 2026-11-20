# ruff: noqa: F811, S101
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, update

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.test_lifecycle import (
    TlFeatureRevision,
    TlIssue,
    TlIssueKind,
    TlIssueStatus,
    TlScope,
)
from app.services.test_lifecycle.git_publish import slugify
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _use_test_db,
    actor,
    scope,
)
from tests.test_tl_workflow import (  # noqa: F401
    VALID_STEPS,
    _do,
    _ready_feature,
    _transition,
    reviewer,
)
from tests.tl_fakes import FakeGitClient, install_fake_git

HUB = "https://hub.test"


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(settings, "tl_hub_base_url", HUB)
    monkeypatch.setattr(settings, "tl_base_branch", "main")
    return install_fake_git(monkeypatch, FakeGitClient())


async def _approved(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    resp = await _do(client, out, "approve", reviewer["email"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def _retry_url(detail):
    return f"{BASE}/features/{detail['id']}/revisions/{detail['working_revision']['id']}/pr"


async def _issues(feature_id, kind=TlIssueKind.PR_CREATION_FAILED):
    async with TestSession() as s:
        return (
            await s.execute(
                select(TlIssue).where(
                    TlIssue.feature_id == uuid.UUID(feature_id), TlIssue.kind == kind
                )
            )
        ).scalars().all()


def test_slugify():
    assert slugify("Creazione di un flusso è OK!") == "Creazione_di_un_flusso_e_OK"
    assert slugify("  --Già  fatto__ ") == "Gia_fatto"
    assert len(slugify("a" * 100)) == 60
    assert not slugify("x" * 59 + " y").endswith("_")
    assert slugify("!!!") == "feature"


async def test_approve_creates_branch_file_and_pr(client, scope, actor, reviewer, fake):
    done = await _approved(client, scope, actor, reviewer)
    code = done["code"]
    path = f"features/ambito/{code}_Pagamento.feature"
    branch = f"qa-hub/{code}-r1"
    rev = done["working_revision"]
    assert done["git_path"] == path
    assert rev["pr_state"] == "open"
    assert rev["pr_number"] == 101
    assert rev["pr_url"] == "https://github.test/org/repo/pull/101"
    assert rev["pr_error"] is None
    assert done["status"] == "designed"

    # il file sul branch e' quello dello snapshot di approvazione
    hist = (await client.get(f"{BASE}/features/{done['id']}/history")).json()
    version = next(h for h in hist if h["kind"] == "version")
    url = f"{BASE}/features/{done['id']}/versions/{version['version_id']}"
    snap = (await client.get(url)).json()
    assert fake.files(branch)[path][0] == snap["snapshot"]["file"]
    assert fake.put_messages == [f"test({scope['code']}): {code} r1"]
    meta = fake.pull_meta[101]
    assert meta["base"] == "main"
    assert meta["title"] == f"[{code}] Pagamento (r1)"
    tag = rev["scenarios"][0]["tag"]
    assert meta["body"] == (
        f"Hub: {HUB}/test-lifecycle/{done['id']}\n"
        f"Autore: {actor['email']}\n"
        f"Approvato da: {reviewer['email']}\n"
        f"Scenari:\n- {tag}\n"
    )
    assert fake.branches[branch] != fake.branches["main"]
    async with TestSession() as s:
        r = await s.get(TlFeatureRevision, uuid.UUID(rev["id"]))
        assert r.pr_branch == branch
        assert r.base_commit_sha == fake.branches["main"]
        events = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.resource_id == done["id"], AuditLog.action == "tl.pr.created"
                )
            )
        ).scalars().all()
        assert len(events) == 1


async def test_git_path_stable_after_title_change(client, scope, actor, reviewer, fake):
    done = await _approved(client, scope, actor, reviewer)
    path = done["git_path"]
    rev_id = uuid.UUID(done["working_revision"]["id"])
    fake.set_pull_state(101, "closed")
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == rev_id)
            .values(pr_state="closed", title="Titolo completamente diverso")
        )
        await s.commit()
    resp = await client.post(_retry_url(done), json={"actor_email": actor["email"]})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["git_path"] == path
    assert out["working_revision"]["pr_state"] == "open"
    assert out["working_revision"]["pr_number"] == 102
    branch = f"qa-hub/{done['code']}-r1"
    assert list(fake.files(branch)) == [path]
    assert "Titolo completamente diverso" in fake.files(branch)[path][0]


async def test_retry_reuses_existing_branch_and_pr(client, scope, actor, reviewer, fake):
    fake.fail_once("create_pull", 502, "bad gateway")
    done = await _approved(client, scope, actor, reviewer)
    branch = f"qa-hub/{done['code']}-r1"
    assert done["working_revision"]["pr_state"] is None
    assert done["working_revision"]["pr_error"]
    assert branch in fake.branches

    resp = await client.post(_retry_url(done), json={"actor_email": actor["email"]})
    assert resp.status_code == 200, resp.text
    rev = resp.json()["working_revision"]
    assert rev["pr_state"] == "open"
    assert rev["pr_error"] is None
    assert len([b for b in fake.branches if b.startswith("qa-hub/")]) == 1
    assert len(fake.pulls) == 1
    assert len(fake.put_messages) == 1  # file gia' identico sul branch: nessun secondo commit
    issues = await _issues(done["id"])
    assert [i.status for i in issues] == [TlIssueStatus.RESOLVED]
    assert resp.json()["open_issues"] == 0


async def test_retry_reuses_open_pull_request(client, scope, actor, reviewer, fake):
    fake.fail_once("put_file", 500, "boom")
    done = await _approved(client, scope, actor, reviewer)
    branch = f"qa-hub/{done['code']}-r1"
    pr = await fake.create_pull(branch, "main", "gia' aperta", "")
    resp = await client.post(_retry_url(done), json={"actor_email": actor["email"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["working_revision"]["pr_number"] == pr.number
    assert len(fake.calls_of("create_pull")) == 1  # solo quella creata dal test
    # il retry scrive il file (il primo put_file era fallito)
    assert len(fake.put_messages) == 1


async def test_pr_failure_keeps_approval_and_opens_issue(client, scope, actor, reviewer, fake):
    fake.fail_once("put_file", 403, "Resource not accessible by personal access token")
    done = await _approved(client, scope, actor, reviewer)
    rev = done["working_revision"]
    assert rev["status"] == "approved"
    assert done["status"] == "designed"
    assert rev["pr_state"] is None
    assert "Resource not accessible" in rev["pr_error"]
    assert done["open_issues"] == 1
    issues = await _issues(done["id"])
    assert len(issues) == 1
    assert issues[0].status == TlIssueStatus.OPEN
    assert issues[0].revision_id == uuid.UUID(rev["id"])

    # un secondo fallimento non duplica l'anomalia
    fake.fail_once("head_sha", 502, "bad gateway")
    resp = await client.post(_retry_url(done), json={"actor_email": actor["email"]})
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "github_error"
    assert resp.json()["detail"]["message"] == "bad gateway"
    assert len(await _issues(done["id"])) == 1


async def test_submit_blocked_without_repo_folder(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    assert _transition(detail, "submit")["allowed"] is True
    async with TestSession() as s:
        await s.execute(
            update(TlScope).where(TlScope.id == uuid.UUID(scope["id"])).values(repo_folder=None)
        )
        await s.commit()
    resp = await client.get(
        f"{BASE}/features/{detail['id']}", params={"actor_email": actor["email"]}
    )
    submit = _transition(resp.json(), "submit")
    assert submit["allowed"] is False
    assert submit["reasons"] == ["L'ambito non ha una cartella del repository configurata"]
    resp = await _do(client, detail, "submit", actor["email"])
    assert resp.status_code == 409


async def test_publish_without_token_returns_503(client, scope, actor, reviewer):
    done = await _approved(client, scope, actor, reviewer)  # nessun token: approvazione ok
    rev = done["working_revision"]
    assert rev["status"] == "approved"
    assert rev["pr_state"] is None
    assert rev["pr_error"] == "Git non configurato: nessun token GitHub"
    assert await _issues(done["id"]) == []
    resp = await client.post(_retry_url(done), json={"actor_email": actor["email"]})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "git_not_configured"


async def test_retry_not_allowed_when_pr_open(client, scope, actor, reviewer, fake):
    done = await _approved(client, scope, actor, reviewer)
    resp = await client.post(_retry_url(done), json={"actor_email": actor["email"]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "pr_not_allowed"


async def test_base_commit_sha_kept_when_branch_exists(client, scope, actor, reviewer, fake):
    fake.fail_once("create_pull", 502, "bad gateway")
    done = await _approved(client, scope, actor, reviewer)  # branch creato, nessuna PR
    rev_id = uuid.UUID(done["working_revision"]["id"])
    first_main = fake.branches["main"]
    fake.seed_file("altro.txt", "x")  # main avanza
    second_main = fake.branches["main"]
    assert second_main != first_main
    # base non nota (la prima pubblicazione e' fallita): si registra il main usato ora
    resp = await client.post(_retry_url(done), json={"actor_email": actor["email"]})
    assert resp.status_code == 200, resp.text
    async with TestSession() as s:
        assert (await s.get(TlFeatureRevision, rev_id)).base_commit_sha == second_main
    # PR chiusa, branch esistente e base gia' nota: resta quella
    fake.set_pull_state(101, "closed")
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == rev_id)
            .values(pr_state="closed")
        )
        await s.commit()
    fake.seed_file("altro2.txt", "y")
    resp = await client.post(_retry_url(done), json={"actor_email": actor["email"]})
    assert resp.status_code == 200, resp.text
    async with TestSession() as s:
        assert (await s.get(TlFeatureRevision, rev_id)).base_commit_sha == second_main


async def test_publish_holds_no_row_lock_and_rejects_concurrent(
    client, scope, actor, reviewer, fake
):
    fake.fail_once("put_file", 500, "boom")
    done = await _approved(client, scope, actor, reviewer)
    started, release = fake.block("put_file")
    first = asyncio.create_task(
        client.post(_retry_url(done), json={"actor_email": actor["email"]})
    )
    await asyncio.wait_for(started.wait(), 10)
    try:
        second = await asyncio.wait_for(
            client.post(_retry_url(done), json={"actor_email": actor["email"]}), 10
        )
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "pr_in_progress"
        # una modifica non correlata sulla stessa feature non aspetta la pubblicazione
        edit = await asyncio.wait_for(
            client.patch(
                f"{BASE}/features/{done['id']}",
                json={"impl_owner_id": actor["id"], "actor_email": actor["email"]},
            ),
            10,
        )
        assert edit.status_code == 200, edit.text
    finally:
        release.set()
    out = await asyncio.wait_for(first, 10)
    assert out.status_code == 200, out.text
    assert out.json()["working_revision"]["pr_state"] == "open"
    assert out.json()["impl_owner_id"] == actor["id"]


async def test_unexpected_error_keeps_approval(client, scope, actor, reviewer, fake):
    async def _boom(branch):
        raise RuntimeError("inatteso")

    fake.head_sha = _boom
    done = await _approved(client, scope, actor, reviewer)
    assert done["working_revision"]["status"] == "approved"
    assert done["working_revision"]["pr_error"] == "Errore imprevisto nella creazione della PR"

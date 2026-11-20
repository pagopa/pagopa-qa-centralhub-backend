# ruff: noqa: F811, S101
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureVersion,
    TlGitSyncState,
    TlIssue,
    TlIssueKind,
    TlStepPhrase,
)
from app.services.test_lifecycle import git_sync
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _use_test_db,
    actor,
    scope,
)
from tests.test_tl_git_publish import _approved
from tests.test_tl_workflow import reviewer  # noqa: F401
from tests.tl_fakes import FakeGitClient, install_fake_git, uninstall_git

PR = 101
SYNC_URL = f"{BASE}/git/sync"
STATUS_URL = f"{BASE}/git/status"


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(settings, "tl_hub_base_url", "")
    monkeypatch.setattr(settings, "tl_base_branch", "main")
    return install_fake_git(monkeypatch, FakeGitClient())


@pytest.fixture(autouse=True)
async def _clean_state(_use_test_db):
    async def wipe():
        async with TestSession() as s:
            await s.execute(delete(TlGitSyncState))
            await s.commit()

    await wipe()
    yield
    await wipe()


async def _sync(fake) -> git_sync.SyncSummary:
    async with TestSession() as s:
        return await git_sync.run_sync(s, fake)


async def _state() -> TlGitSyncState | None:
    async with TestSession() as s:
        return await s.get(TlGitSyncState, 1)


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


async def _issues(fid, kind) -> list[TlIssue]:
    async with TestSession() as s:
        return list(
            (
                await s.execute(
                    select(TlIssue).where(
                        TlIssue.feature_id == uuid.UUID(str(fid)), TlIssue.kind == kind
                    )
                )
            ).scalars()
        )


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


async def test_first_run_records_head_without_import(client, scope, actor, reviewer, fake):
    done = await _approved(client, scope, actor, reviewer)
    fake.seed_file(done["git_path"], "# qa-hub: X\nFunzionalità: Boh\n")
    head = fake.branches["main"]
    summary = await _sync(fake)
    assert summary.head_sha == head
    assert summary.files_imported == 0
    assert fake.calls_of("compare") == []
    state = await _state()
    assert state.last_main_sha == head
    assert state.last_error is None
    assert state.last_run_at is not None
    assert state.last_summary["head_sha"] == head


async def test_open_pr_merged_marks_revision(client, scope, actor, reviewer, fake):
    done = await _approved(client, scope, actor, reviewer)
    await _sync(fake)  # primo giro
    sha = fake.merge_pull(PR)
    summary = await _sync(fake)
    assert summary.prs_merged == 1
    rev = await _rev(done["working_revision"]["id"])
    assert rev.pr_state == "merged"
    assert rev.merged_commit_sha == sha
    assert (await _state()).last_main_sha == sha


async def test_open_pr_closed_opens_issue(client, scope, actor, reviewer, fake):
    done = await _approved(client, scope, actor, reviewer)
    fake.set_pull_state(PR, "closed")
    summary = await _sync(fake)
    assert summary.prs_closed == 1
    assert summary.issues_opened == 1
    rev = await _rev(done["working_revision"]["id"])
    assert rev.pr_state == "closed"
    issues = await _issues(done["id"], TlIssueKind.PR_CLOSED_UNMERGED)
    assert len(issues) == 1
    assert issues[0].revision_id == rev.id
    assert len(await _events(done["id"], "tl.pr.closed")) == 1
    # un secondo giro non riapre nulla
    again = await _sync(fake)
    assert again.prs_closed == 0
    assert len(await _issues(done["id"], TlIssueKind.PR_CLOSED_UNMERGED)) == 1


async def test_changed_feature_file_is_imported(client, scope, actor, reviewer, fake):
    done = await _approved(client, scope, actor, reviewer)
    fake.merge_pull(PR)
    await _sync(fake)  # registra il merge e salva head
    path = done["git_path"]
    text = fake.files("main")[path][0]
    fake.seed_file(path, text.replace("Quando paga\n", "Quando paga con la carta\n"))
    summary = await _sync(fake)
    assert summary.files_imported == 1
    assert [v.origin for v in await _versions(done["id"])] == ["hub", "git"]
    assert fake.files("main")  # head avanzato salvato
    assert (await _state()).last_main_sha == fake.branches["main"]


async def test_unmanaged_file_is_ignored(client, fake):
    await _sync(fake)
    fake.seed_file("docs/note.feature", "Funzionalità: senza header\n  Scenario: x\n")
    fake.seed_file("README.md", "ciao")
    summary = await _sync(fake)
    assert summary.issues_opened == 0
    async with TestSession() as s:
        n = (await s.execute(select(TlIssue).where(TlIssue.path == "docs/note.feature"))).all()
    assert n == []
    assert (await _state()).last_main_sha == fake.branches["main"]


async def test_removed_file_opens_file_deleted(client, scope, actor, reviewer, fake):
    done = await _approved(client, scope, actor, reviewer)
    fake.merge_pull(PR)
    await _sync(fake)
    fake.remove_file(done["git_path"])
    summary = await _sync(fake)
    assert summary.issues_opened == 1
    assert len(await _issues(done["id"], TlIssueKind.FILE_DELETED)) == 1


async def test_step_index_rebuilt_when_feature_changes(client, fake):
    await _sync(fake)
    fake.seed_file(
        "src/x/a.feature",
        "# qa-hub: ZZZ\n# language: it\nFunzionalità: A\n  Scenario: s\n    Dato uno step unico per indice\n",
    )
    summary = await _sync(fake)
    assert summary.steps_indexed is True
    async with TestSession() as s:
        rows = (
            await s.execute(
                select(TlStepPhrase).where(TlStepPhrase.text == "uno step unico per indice")
            )
        ).all()
    assert len(rows) == 1
    fake.seed_file("README.md", "x")
    assert (await _sync(fake)).steps_indexed is False


async def test_failure_records_error_and_keeps_sha(client, fake):
    first = await _sync(fake)
    fake.seed_file("src/a.feature", "x")
    fake.fail_once("compare", 502, "GitHub giù")
    with pytest.raises(Exception, match="GitHub"):
        await _sync(fake)
    state = await _state()
    assert state.last_main_sha == first.head_sha  # il prossimo giro riprova lo stesso range
    assert "GitHub giù" in state.last_error
    summary = await _sync(fake)
    assert summary.head_sha == fake.branches["main"]
    assert (await _state()).last_error is None


async def test_concurrent_sync_second_gets_409(client, actor, fake):
    await _sync(fake)
    started, release = fake.block("head_sha")
    first = asyncio.create_task(client.post(SYNC_URL, json={"actor_email": actor["email"]}))
    await asyncio.wait_for(started.wait(), 10)
    try:
        second = await asyncio.wait_for(
            client.post(SYNC_URL, json={"actor_email": actor["email"]}), 10
        )
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "sync_in_progress"
        with pytest.raises(git_sync.SyncInProgress):
            await _sync(fake)
    finally:
        release.set()
    out = await asyncio.wait_for(first, 10)
    assert out.status_code == 200, out.text


async def test_sync_endpoint_returns_summary(client, actor, fake):
    resp = await client.post(SYNC_URL, json={"actor_email": actor["email"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["head_sha"] == fake.branches["main"]
    assert set(body) == {
        "prs_merged", "prs_closed", "files_imported", "issues_opened", "steps_indexed", "head_sha",
    }


async def test_sync_unknown_actor_403(client, fake):
    resp = await client.post(SYNC_URL, json={"actor_email": "nessuno@example.test"})
    assert resp.status_code == 403


async def test_sync_github_error_returns_502(client, actor, fake):
    fake.fail_once("head_sha", 500, "boom")
    resp = await client.post(SYNC_URL, json={"actor_email": actor["email"]})
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "github_error"
    assert "boom" in (await _state()).last_error


async def test_sync_without_token_returns_503(client, actor, monkeypatch):
    uninstall_git(monkeypatch)
    resp = await client.post(SYNC_URL, json={"actor_email": actor["email"]})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "git_not_configured"


async def test_status_endpoint(client, actor, fake, monkeypatch):
    resp = await client.get(STATUS_URL)
    assert resp.status_code == 200
    assert resp.json() == {
        "configured": True,
        "last_main_sha": None,
        "last_run_at": None,
        "last_error": None,
        "last_summary": None,
    }
    await client.post(SYNC_URL, json={"actor_email": actor["email"]})
    body = (await client.get(STATUS_URL)).json()
    assert body["last_main_sha"] == fake.branches["main"]
    assert body["last_run_at"] is not None
    assert body["last_summary"]["head_sha"] == fake.branches["main"]
    uninstall_git(monkeypatch)
    assert (await client.get(STATUS_URL)).json()["configured"] is False


async def test_task_exits_silently_without_token(monkeypatch):
    from app.tasks import sync_test_lifecycle as task_mod

    uninstall_git(monkeypatch)
    assert (await task_mod._async_sync())["status"] == "not_configured"


def test_beat_entry():
    from app.tasks import schedule

    entry = schedule.celery_app.conf.beat_schedule["sync-test-lifecycle-git"]
    assert entry["task"] == "app.tasks.sync_test_lifecycle.sync_test_lifecycle_git"
    assert entry["schedule"] == settings.tl_sync_interval_s
    assert entry["options"] == {"expires": settings.tl_sync_interval_s}


@pytest.mark.parametrize("env", ["dev", "uat", "prod"])
def test_worker_helm_mounts_github_token(env):
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parents[1] / "helm" / f"values-worker-{env}.yaml"
    secrets = yaml.safe_load(path.read_text())["microservice-chart"]["envSecret"]
    assert secrets["TAS_GITHUB_TOKEN"] == "pagopa-platform-tas-github-bot-pat"

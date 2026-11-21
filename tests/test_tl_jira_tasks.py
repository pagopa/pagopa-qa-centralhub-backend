# ruff: noqa: S101
"""Integrazione Jira del Test Lifecycle (fase 3): verifica chiavi, cache, task, Epic QA."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.test_lifecycle import TlFeature
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _use_test_db,
    actor,
    scope,
)
from tests.tl_fakes import FakeJiraClient, install_fake_jira


@pytest.fixture
def jira(monkeypatch):
    fake = install_fake_jira(monkeypatch, FakeJiraClient())
    fake.add_issue(
        "PQ-495", "[QA Epic] FdR", issue_type="Epic", status="In Progress", category="indeterminate"
    )
    fake.add_issue(
        "PQ-12", "Progettazione esistente", issue_type="Progettazione Scenari e casi di test"
    )
    fake.add_issue("CHK-7", "Story di prodotto", issue_type="Story")
    fake.add_issue("ABC-1", "Epic di prodotto", issue_type="Epic")  # usata da _ready_feature
    return fake


async def _patch_feature(client, feat, actor, **fields):
    return await client.patch(
        f"{BASE}/features/{feat['id']}", json={"actor_email": actor["email"], **fields}
    )


async def _feature_row(fid) -> TlFeature:
    async with TestSession() as s:
        return await s.get(TlFeature, uuid.UUID(fid))


def _link(detail, role):
    return next((j for j in detail["jira"] if j["role"] == role), None)


# ── Task 3: verifica e cache ──────────────────────────────────────────────────


async def test_save_existing_key_caches_status(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    resp = await _patch_feature(client, feat, actor, jira_design_key="PQ-12")
    assert resp.status_code == 200, resp.text
    design = _link(resp.json(), "design")
    assert design["key"] == "PQ-12"
    assert design["verified"] is True
    assert design["summary"] == "Progettazione esistente"
    assert design["category"] == "new"
    assert design["url"] == "https://jira.test/browse/PQ-12"
    row = await _feature_row(feat["id"])
    assert row.jira_cache["PQ-12"]["status"] == "Backlog"


async def test_save_missing_key_returns_422(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    resp = await _patch_feature(client, feat, actor, jira_design_key="PQ-999999")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "jira_issue_not_found"
    assert resp.json()["detail"]["key"] == "PQ-999999"
    assert (await _feature_row(feat["id"])).jira_design_key is None


async def test_save_key_when_jira_down_is_accepted_unverified(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    jira.fail_once("get_issue", 502, "Jira non raggiungibile")
    resp = await _patch_feature(client, feat, actor, jira_design_key="PQ-12")
    assert resp.status_code == 200, resp.text
    design = _link(resp.json(), "design")
    assert design["key"] == "PQ-12" and design["verified"] is False


async def test_save_key_without_jira_config_is_accepted_unverified(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    resp = await _patch_feature(client, feat, actor, jira_design_key="PQ-12")
    assert resp.status_code == 200, resp.text
    assert _link(resp.json(), "design")["verified"] is False


async def test_feature_epic_must_be_an_epic(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    resp = await _patch_feature(client, feat, actor, jira_qa_epic_key="PQ-12")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "jira_not_an_epic"
    resp = await _patch_feature(client, feat, actor, jira_qa_epic_key="PQ-495")
    assert resp.status_code == 200, resp.text
    epic = _link(resp.json(), "qa_epic")
    assert epic["key"] == "PQ-495" and epic["verified"] is True


async def test_product_key_verified_on_revision_update(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    rev = feat["working_revision"]
    url = f"{BASE}/features/{feat['id']}/revisions/{rev['id']}"
    resp = await client.patch(
        url,
        json={
            "lock_version": rev["lock_version"],
            "product_issue_key": "ABC-404",
            "actor_email": actor["email"],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "jira_issue_not_found"
    resp = await client.patch(
        url,
        json={
            "lock_version": rev["lock_version"],
            "product_issue_key": "CHK-7",
            "actor_email": actor["email"],
        },
    )
    assert resp.status_code == 200, resp.text
    detail = (await client.get(f"{BASE}/features/{feat['id']}")).json()
    product = _link(detail, "product")
    assert product["key"] == "CHK-7" and product["summary"] == "Story di prodotto"


async def test_detail_exposes_jira_links_with_effective_epic(client, actor, jira):
    code = "T" + uuid.uuid4().hex[:8]
    resp = await client.post(
        f"{BASE}/scopes",
        json={"code": code, "name": "Con epic", "repo_folder": "f", "jira_qa_epic_key": "PQ-495"},
    )
    assert resp.status_code == 201, resp.text
    scope_ = resp.json()
    feat = await _new_feature(client, scope_, actor)
    detail = (await client.get(f"{BASE}/features/{feat['id']}")).json()
    assert detail["jira_qa_epic_effective"] == "PQ-495"
    epic = _link(detail, "qa_epic")
    assert epic["key"] == "PQ-495" and epic["summary"] == "[QA Epic] FdR"
    assert _link(detail, "design") is None


async def test_refresh_updates_cache_and_event_only_on_change(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    await _patch_feature(client, feat, actor, jira_design_key="PQ-12")
    url = f"{BASE}/features/{feat['id']}/jira/refresh"
    resp = await client.post(url, json={"actor_email": actor["email"]})
    assert resp.status_code == 200, resp.text
    jira.add_issue(
        "PQ-12",
        "Progettazione esistente",
        issue_type="Progettazione Scenari e casi di test",
        status="Done",
        category="done",
    )
    resp = await client.post(url, json={"actor_email": actor["email"]})
    assert _link(resp.json(), "design")["category"] == "done"
    async with TestSession() as s:
        events = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.resource_id == feat["id"], AuditLog.action == "tl.jira.refreshed"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1


async def test_scope_existing_epic_verified_on_save(client, cleanup_scopes, jira):
    code = "T" + uuid.uuid4().hex[:8]
    resp = await client.post(
        f"{BASE}/scopes", json={"code": code, "name": "x", "jira_qa_epic_key": "PQ-404"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "jira_issue_not_found"
    resp = await client.post(
        f"{BASE}/scopes", json={"code": code, "name": "x", "jira_qa_epic_key": "PQ-495"}
    )
    assert resp.status_code == 201, resp.text
    cleanup_scopes.append(resp.json()["id"])


async def test_scope_epic_not_an_epic_422(client, cleanup_scopes, jira):
    code = "T" + uuid.uuid4().hex[:8]
    resp = await client.post(f"{BASE}/scopes", json={"code": code, "name": "x"})
    cleanup_scopes.append(resp.json()["id"])
    resp = await client.patch(
        f"{BASE}/scopes/{resp.json()['id']}", json={"jira_qa_epic_key": "PQ-12"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "jira_not_an_epic"


@pytest.fixture
async def cleanup_scopes():
    from sqlalchemy import delete

    from app.models.test_lifecycle import TlScope

    ids: list[str] = []
    yield ids
    async with TestSession() as s:
        for sid in ids:
            await s.execute(delete(TlScope).where(TlScope.id == uuid.UUID(sid)))
        await s.commit()


# ── Task 4: task Progettazione ed Epic QA ─────────────────────────────────────


async def _jira_scope(client, scope_, jira, epic="PQ-495", component="FDR"):
    resp = await client.patch(
        f"{BASE}/scopes/{scope_['id']}",
        json={"jira_qa_epic_key": epic, "jira_component": component},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _design(client, feat, actor):
    return await client.post(
        f"{BASE}/features/{feat['id']}/jira/design-task", json={"actor_email": actor["email"]}
    )


async def test_create_design_task_payload(client, scope, actor, jira, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "tl_hub_base_url", "https://hub.test")
    await _jira_scope(client, scope, jira)
    jira.users[actor["email"].lower()] = "acc-owner"
    feat = await _new_feature(client, scope, actor, title="Pagamento", design_owner_id=actor["id"])
    rev = feat["working_revision"]
    await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev['id']}",
        json={"lock_version": rev["lock_version"], "product_issue_key": "CHK-7",
              "actor_email": actor["email"]},
    )
    resp = await _design(client, feat, actor)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    [created] = jira.created
    assert created["project"] == "PQ"
    assert created["issue_type"] == "Progettazione Scenari e casi di test"
    assert created["summary"] == f"[{feat['code']}] Progettazione — Pagamento"
    assert created["labels"] == ["testing"]
    assert created["parent_key"] == "PQ-495"
    assert created["component"] == "FDR"
    assert created["assignee_id"] == "acc-owner"
    assert f"https://hub.test/test-lifecycle/{feat['id']}" in str(created["description_adf"])
    assert jira.links == [("Relates", created["key"], "CHK-7")]
    assert body["jira_design_key"] == created["key"]
    assert _link(body, "design")["verified"] is True
    assert body["jira_warning"] is None


async def test_feature_epic_overrides_scope(client, scope, actor, jira):
    jira.add_issue("PQ-777", "[QA Epic] Altro", issue_type="Epic")
    await _jira_scope(client, scope, jira)
    feat = await _new_feature(client, scope, actor)
    await _patch_feature(client, feat, actor, jira_qa_epic_key="PQ-777")
    assert (await _design(client, feat, actor)).status_code == 200
    assert jira.created[0]["parent_key"] == "PQ-777"


async def test_create_reuses_existing_task_found_by_jql(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor, title="Pagamento")
    jira.add_issue("PQ-555", f"[{feat['code']}] Progettazione — Pagamento",
                   issue_type="Progettazione Scenari e casi di test")
    resp = await _design(client, feat, actor)
    assert resp.status_code == 200, resp.text
    assert jira.created == []
    assert resp.json()["jira_design_key"] == "PQ-555"


async def test_unknown_assignee_creates_unassigned_with_warning(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor, design_owner_id=actor["id"])
    resp = await _design(client, feat, actor)
    assert resp.status_code == 200, resp.text
    assert jira.created[0]["assignee_id"] is None
    assert "non trovato su Jira" in resp.json()["jira_warning"]


async def test_jira_400_surfaces_message_and_keeps_feature_unchanged(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    jira.fail_once("create_issue", 400, "parent: Issue PQ-495 is not an epic")
    resp = await _design(client, feat, actor)
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "jira_error"
    assert "not an epic" in resp.json()["detail"]["message"]
    row = await _feature_row(feat["id"])
    assert row.jira_design_key is None
    assert "not an epic" in row.jira_error


async def test_concurrent_create_second_gets_409(client, scope, actor, jira):
    import asyncio

    feat = await _new_feature(client, scope, actor)
    started, release = jira.block("create_issue")
    first = asyncio.create_task(_design(client, feat, actor))
    await asyncio.wait_for(started.wait(), 10)
    try:
        second = await asyncio.wait_for(_design(client, feat, actor), 10)
    finally:
        release.set()
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "jira_in_progress"
    assert (await first).status_code == 200
    assert len(jira.created) == 1


async def test_design_task_not_allowed_outside_draft(client, scope, actor, jira):
    from sqlalchemy import update

    from app.models.test_lifecycle import TlFeatureRevision, TlRevisionStatus

    feat = await _new_feature(client, scope, actor)
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == uuid.UUID(feat["working_revision"]["id"]))
            .values(status=TlRevisionStatus.IN_REVIEW)
        )
        await s.commit()
    resp = await _design(client, feat, actor)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "jira_task_not_allowed"


async def test_design_task_exists_409(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    await _patch_feature(client, feat, actor, jira_design_key="PQ-12")
    resp = await _design(client, feat, actor)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "jira_task_exists"


async def test_without_jira_config_503(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    resp = await _design(client, feat, actor)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "jira_not_configured"


async def _epic(client, scope_, actor):
    return await client.post(
        f"{BASE}/scopes/{scope_['id']}/jira/epic", json={"actor_email": actor["email"]}
    )


async def test_create_scope_epic_payload_and_saves_key(client, scope, actor, jira):
    await client.patch(f"{BASE}/scopes/{scope['id']}", json={"jira_component": "FDR"})
    resp = await _epic(client, scope, actor)
    assert resp.status_code == 200, resp.text
    [created] = jira.created
    assert created["issue_type"] == "Epic"
    assert created["summary"] == f"[QA Epic] {scope['name']}"
    assert created["parent_key"] is None and created["assignee_id"] is None
    assert created["component"] == "FDR" and created["labels"] == ["testing"]
    assert resp.json()["jira_qa_epic_key"] == created["key"]


async def test_scope_epic_reuses_existing_by_summary(client, scope, actor, jira):
    jira.add_issue("PQ-600", f"[QA Epic] {scope['name']}", issue_type="Epic")
    resp = await _epic(client, scope, actor)
    assert resp.status_code == 200, resp.text
    assert jira.created == [] and resp.json()["jira_qa_epic_key"] == "PQ-600"


async def test_scope_epic_exists_409(client, scope, actor, jira):
    await _jira_scope(client, scope, jira)
    resp = await _epic(client, scope, actor)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "jira_task_exists"


async def test_concurrent_epic_create_second_gets_409(client, scope, actor, jira):
    import asyncio

    started, release = jira.block("create_issue")
    first = asyncio.create_task(_epic(client, scope, actor))
    await asyncio.wait_for(started.wait(), 10)
    try:
        second = await asyncio.wait_for(_epic(client, scope, actor), 10)
    finally:
        release.set()
    assert second.status_code == 409
    assert (await first).status_code == 200
    assert len(jira.created) == 1


async def test_epics_search_endpoint(client, jira):
    resp = await client.get(f"{BASE}/jira/epics", params={"q": "fdr"})
    assert resp.status_code == 200, resp.text
    assert [e["key"] for e in resp.json()] == ["PQ-495"]
    assert resp.json()[0]["summary"] == "[QA Epic] FdR"


# ── Task 5: task Implementazione all'approvazione ─────────────────────────────


async def _approved_with_jira(client, scope_, actor, jira, impl_owner=None):
    from tests.test_tl_workflow import _do, _ready_feature

    reviewer = await _make_user("rev")
    detail = await _ready_feature(client, scope_, actor, reviewer)
    if impl_owner:
        await _patch_feature(client, detail, actor, impl_owner_id=impl_owner["id"])
    detail = (await _do(client, detail, "submit", actor["email"])).json()
    resp = await _do(client, detail, "approve", reviewer["email"])
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _make_user(prefix):
    from app.models.user import User

    email = f"tl-{prefix}-{uuid.uuid4().hex[:8]}@example.test"
    async with TestSession() as s:
        user = User(email=email, name=prefix, role="qa_analyst", is_active=True)
        s.add(user)
        await s.commit()
        return {"id": str(user.id), "email": email}


async def _impl_issues(fid):
    from app.models.test_lifecycle import TlIssue, TlIssueKind

    async with TestSession() as s:
        return (
            await s.execute(
                select(TlIssue).where(
                    TlIssue.feature_id == uuid.UUID(fid),
                    TlIssue.kind == TlIssueKind.JIRA_CREATION_FAILED,
                )
            )
        ).scalars().all()


async def test_approve_creates_impl_task_and_link(client, scope, actor, jira):
    await _jira_scope(client, scope, jira)
    engineer = await _make_user("eng")
    jira.users[engineer["email"].lower()] = "acc-eng"
    done = await _approved_with_jira(client, scope, actor, jira, impl_owner=engineer)
    impl = [c for c in jira.created if c["issue_type"] == "Implementazione suite di TA"]
    assert len(impl) == 1
    assert impl[0]["summary"].startswith(f"[{done['code']}] Implementazione suite — ")
    assert impl[0]["assignee_id"] == "acc-eng"
    assert impl[0]["parent_key"] == "PQ-495"
    row = await _feature_row(done["id"])
    assert row.jira_impl_key == impl[0]["key"]
    assert row.jira_error is None
    assert ("Relates", impl[0]["key"], "ABC-1") in jira.links


async def test_approve_jira_failure_keeps_approval_and_opens_issue(client, scope, actor, jira):
    jira.fail_once("create_issue", 500, "Jira giù")
    done = await _approved_with_jira(client, scope, actor, jira)
    assert done["working_revision"]["status"] == "approved"
    row = await _feature_row(done["id"])
    assert row.jira_impl_key is None and "Jira giù" in row.jira_error
    issues = await _impl_issues(done["id"])
    assert len(issues) == 1 and issues[0].status.value == "open"


async def test_retry_impl_task_resolves_issue(client, scope, actor, jira):
    jira.fail_once("create_issue", 500, "Jira giù")
    done = await _approved_with_jira(client, scope, actor, jira)
    resp = await client.post(
        f"{BASE}/features/{done['id']}/jira/impl-task", json={"actor_email": actor["email"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["jira_impl_key"]
    assert resp.json()["jira_error"] is None
    assert [i.status.value for i in await _impl_issues(done["id"])] == ["resolved"]


async def test_approve_without_jira_config_sets_error_only(client, scope, actor, monkeypatch):
    from tests.tl_fakes import uninstall_jira

    uninstall_jira(monkeypatch)
    done = await _approved_with_jira(client, scope, actor, None)
    row = await _feature_row(done["id"])
    assert row.jira_error == "Jira non configurato"
    assert await _impl_issues(done["id"]) == []


async def test_reapproval_does_not_create_second_impl_task(client, scope, actor, jira):
    done = await _approved_with_jira(client, scope, actor, jira)
    count = len([c for c in jira.created if c["issue_type"] == "Implementazione suite di TA"])
    resp = await client.post(
        f"{BASE}/features/{done['id']}/jira/impl-task", json={"actor_email": actor["email"]}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "jira_task_exists"
    assert len([c for c in jira.created if c["issue_type"] == "Implementazione suite di TA"]) == count

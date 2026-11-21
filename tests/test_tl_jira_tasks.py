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

# ruff: noqa: S101
"""Test Lifecycle: delete previews and deletion of features and scopes."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from app.models.test_lifecycle import TlFeature, TlFeatureRevision, TlIssue, TlIssueKind
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _new_scenario,
    _use_test_db,
    actor,
    scope,
)
from tests.tl_fakes import FakeJiraClient, install_fake_jira, uninstall_jira


@pytest.fixture
def jira(monkeypatch):
    fake = install_fake_jira(monkeypatch, FakeJiraClient())
    for key in ("PQ-1", "PQ-2", "PQ-3", "PQ-4"):
        fake.add_issue(key, f"Issue {key}")
    fake.add_issue("PQ-90", "Epic QA", issue_type="Epic")
    return fake


async def _set(feature_id: str, **fields):
    async with TestSession() as s:
        await s.execute(update(TlFeature).where(TlFeature.id == uuid.UUID(feature_id)).values(**fields))
        await s.commit()


async def _link(feature_id: str, design: str | None = None, impl: str | None = None):
    cache = {k: {"summary": f"Sum {k}", "status": "Backlog", "category": "new"} for k in (design, impl) if k}
    await _set(feature_id, jira_design_key=design, jira_impl_key=impl, jira_cache=cache)


async def _exists(feature_id: str) -> bool:
    async with TestSession() as s:
        return await s.get(TlFeature, uuid.UUID(feature_id)) is not None


async def _preview(client, kind: str, entity_id: str):
    return await client.get(f"{BASE}/{kind}/{entity_id}/delete-preview")


# ── previews ──────────────────────────────────────────────────────────────────


async def test_feature_preview_draft_is_deletable_and_lists_design_and_impl(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    await _link(feat["id"], "PQ-1", "PQ-2")
    resp = await _preview(client, "features", feat["id"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["deletable"] is True
    assert data["blockers"] == []
    issues = {i["key"]: i for i in data["jira_issues"]}
    assert set(issues) == {"PQ-1", "PQ-2"}
    assert issues["PQ-1"]["role"] == "design" and issues["PQ-1"]["default_selected"] is True
    assert issues["PQ-2"]["role"] == "impl" and issues["PQ-2"]["summary"] == "Sum PQ-2"
    assert data["features"][0]["code"] == feat["code"]


async def test_feature_preview_published_is_blocked(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    await _set(feat["id"], git_path="features/x.feature")
    data = (await _preview(client, "features", feat["id"])).json()
    assert data["deletable"] is False
    assert [b["reason"] for b in data["blockers"]] == ["published"]


async def test_feature_preview_open_pr_is_blocked(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == uuid.UUID(feat["working_revision"]["id"]))
            .values(pr_state="open")
        )
        await s.commit()
    data = (await _preview(client, "features", feat["id"])).json()
    assert data["deletable"] is False
    assert [b["reason"] for b in data["blockers"]] == ["pr_open"]


async def test_scope_preview_lists_epic_unselected_and_dedups_shared_key(client, scope, actor, jira):
    f1 = await _new_feature(client, scope, actor, title="Uno")
    f2 = await _new_feature(client, scope, actor, title="Due")
    await _link(f1["id"], "PQ-1", "PQ-2")
    await _link(f2["id"], "PQ-1", None)
    resp = await client.patch(f"{BASE}/scopes/{scope['id']}", json={"jira_qa_epic_key": "PQ-90"})
    assert resp.status_code == 200, resp.text
    data = (await _preview(client, "scopes", scope["id"])).json()
    keys = [i["key"] for i in data["jira_issues"]]
    assert sorted(keys) == ["PQ-1", "PQ-2", "PQ-90"]
    epic = next(i for i in data["jira_issues"] if i["key"] == "PQ-90")
    assert epic["role"] == "qa_epic" and epic["default_selected"] is False and epic["feature_code"] is None
    assert len(data["features"]) == 2


async def test_scope_preview_blocked_names_blocking_features(client, scope, actor):
    f1 = await _new_feature(client, scope, actor, title="Uno")
    await _new_feature(client, scope, actor, title="Due")
    await _set(f1["id"], git_path="features/x.feature")
    data = (await _preview(client, "scopes", scope["id"])).json()
    assert data["deletable"] is False
    assert [b["code"] for b in data["blockers"]] == [f1["code"]]


async def test_scope_preview_empty_scope_is_deletable(client, scope):
    data = (await _preview(client, "scopes", scope["id"])).json()
    assert data["deletable"] is True and data["features"] == [] and data["jira_issues"] == []


async def test_preview_unknown_id_returns_404(client):
    missing = str(uuid.uuid4())
    assert (await _preview(client, "features", missing)).status_code == 404
    assert (await _preview(client, "scopes", missing)).status_code == 404


async def test_list_endpoints_expose_deletable_flag(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    listed = (await client.get(f"{BASE}/features", params={"scope_id": scope["id"]})).json()
    assert listed[0]["deletable"] is True
    assert next(s for s in (await client.get(f"{BASE}/scopes")).json() if s["id"] == scope["id"])["deletable"]
    await _set(feat["id"], git_path="features/x.feature")
    listed = (await client.get(f"{BASE}/features", params={"scope_id": scope["id"]})).json()
    assert listed[0]["deletable"] is False
    scopes = (await client.get(f"{BASE}/scopes")).json()
    assert next(s for s in scopes if s["id"] == scope["id"])["deletable"] is False


# ── delete feature ────────────────────────────────────────────────────────────


async def _delete(client, kind: str, entity_id: str, actor, keys: list[str] | None = None):
    return await client.request(
        "DELETE",
        f"{BASE}/{kind}/{entity_id}",
        json={"actor_email": actor["email"], "delete_jira_keys": keys or []},
    )


async def test_delete_feature_removes_rows_and_selected_jira(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    await _new_scenario(client, feat, actor)
    await _link(feat["id"], "PQ-1", "PQ-2")
    resp = await _delete(client, "features", feat["id"], actor, ["PQ-1"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted_features"] == 1
    assert body["jira_results"] == [{"key": "PQ-1", "status": "deleted", "reason": None}]
    assert not await _exists(feat["id"])
    assert [c[1] for c in jira.calls_of("delete_issue")] == ["PQ-1"]
    assert "PQ-2" in jira.issues


async def test_delete_feature_without_keys_never_calls_jira(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    await _link(feat["id"], "PQ-1", "PQ-2")
    resp = await _delete(client, "features", feat["id"], actor)
    assert resp.status_code == 200
    assert jira.calls_of("delete_issue") == []
    assert not await _exists(feat["id"])


async def test_delete_published_feature_returns_409_with_blockers(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    await _set(feat["id"], git_path="features/x.feature")
    resp = await _delete(client, "features", feat["id"], actor)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "feature_not_deletable"
    assert detail["blockers"][0]["reason"] == "published"
    assert await _exists(feat["id"])


async def test_delete_feature_open_pr_returns_409(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == uuid.UUID(feat["working_revision"]["id"]))
            .values(pr_state="open")
        )
        await s.commit()
    resp = await _delete(client, "features", feat["id"], actor)
    assert resp.status_code == 409
    assert resp.json()["detail"]["blockers"][0]["reason"] == "pr_open"


async def test_delete_feature_unlinked_key_returns_422_and_deletes_nothing(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    await _link(feat["id"], "PQ-1", None)
    resp = await _delete(client, "features", feat["id"], actor, ["PQ-1", "PQ-3"])
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "jira_key_not_linked"
    assert await _exists(feat["id"])
    assert jira.calls_of("delete_issue") == []


async def test_delete_feature_twice_returns_404_second_time_and_no_jira_call(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    await _link(feat["id"], "PQ-1", None)
    assert (await _delete(client, "features", feat["id"], actor, ["PQ-1"])).status_code == 200
    again = await _delete(client, "features", feat["id"], actor, ["PQ-1"])
    assert again.status_code == 404
    assert len(jira.calls_of("delete_issue")) == 1


async def test_delete_feature_jira_partial_failure_reports_per_key(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    await _link(feat["id"], "PQ-1", "PQ-2")
    jira.fail_once("delete_issue", 403, "forbidden")
    resp = await _delete(client, "features", feat["id"], actor, ["PQ-1", "PQ-2"])
    assert resp.status_code == 200
    results = {r["key"]: r for r in resp.json()["jira_results"]}
    assert results["PQ-1"]["status"] == "failed" and "forbidden" in results["PQ-1"]["reason"]
    assert results["PQ-2"]["status"] == "deleted"
    assert not await _exists(feat["id"])


async def test_delete_feature_jira_not_configured_returns_503_and_keeps_feature(
    client, scope, actor, jira, monkeypatch
):
    feat = await _new_feature(client, scope, actor)
    await _link(feat["id"], "PQ-1", None)
    uninstall_jira(monkeypatch)
    resp = await _delete(client, "features", feat["id"], actor, ["PQ-1"])
    assert resp.status_code == 503
    assert await _exists(feat["id"])


async def test_delete_feature_unknown_actor_returns_403(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    resp = await _delete(client, "features", feat["id"], {"email": "nobody@example.test"})
    assert resp.status_code == 403
    assert await _exists(feat["id"])


async def test_delete_feature_removes_open_anomalies(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    async with TestSession() as s:
        s.add(TlIssue(feature_id=uuid.UUID(feat["id"]), kind=TlIssueKind.FILE_DELETED, detail={}))
        await s.commit()
    assert (await _delete(client, "features", feat["id"], actor)).status_code == 200
    async with TestSession() as s:
        left = (
            await s.execute(select(TlIssue).where(TlIssue.feature_id == uuid.UUID(feat["id"])))
        ).scalars().all()
    assert left == []


# ── delete scope ──────────────────────────────────────────────────────────────


async def test_delete_empty_scope(client, scope, actor):
    resp = await _delete(client, "scopes", scope["id"], actor)
    assert resp.status_code == 200
    assert resp.json()["deleted_features"] == 0
    assert all(s["id"] != scope["id"] for s in (await client.get(f"{BASE}/scopes")).json())


async def test_delete_scope_with_features_removes_all_and_deletes_selected_jira(client, scope, actor, jira):
    f1 = await _new_feature(client, scope, actor, title="Uno")
    f2 = await _new_feature(client, scope, actor, title="Due")
    for f in (f1, f2):
        await _new_scenario(client, f, actor)
    await _link(f1["id"], "PQ-1", None)
    await _link(f2["id"], "PQ-2", None)
    assert (await client.patch(f"{BASE}/scopes/{scope['id']}", json={"jira_qa_epic_key": "PQ-90"})).status_code == 200
    resp = await _delete(client, "scopes", scope["id"], actor, ["PQ-1", "PQ-2"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted_features"] == 2
    assert not await _exists(f1["id"]) and not await _exists(f2["id"])
    assert "PQ-90" in jira.issues
    assert sorted(c[1] for c in jira.calls_of("delete_issue")) == ["PQ-1", "PQ-2"]


async def test_delete_scope_blocked_when_any_feature_published(client, scope, actor, jira):
    f1 = await _new_feature(client, scope, actor, title="Uno")
    f2 = await _new_feature(client, scope, actor, title="Due")
    await _set(f1["id"], git_path="features/x.feature")
    resp = await _delete(client, "scopes", scope["id"], actor)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "scope_not_deletable"
    assert [b["code"] for b in detail["blockers"]] == [f1["code"]]
    assert await _exists(f1["id"]) and await _exists(f2["id"])


async def test_delete_scope_can_delete_epic_when_selected(client, scope, actor, jira):
    assert (await client.patch(f"{BASE}/scopes/{scope['id']}", json={"jira_qa_epic_key": "PQ-90"})).status_code == 200
    resp = await _delete(client, "scopes", scope["id"], actor, ["PQ-90"])
    assert resp.status_code == 200
    assert "PQ-90" not in jira.issues


async def test_delete_scope_rejects_feature_level_override_epic_key(client, scope, actor, jira):
    feat = await _new_feature(client, scope, actor)
    await _set(feat["id"], jira_qa_epic_key="PQ-90")
    resp = await _delete(client, "scopes", scope["id"], actor, ["PQ-90"])
    assert resp.status_code == 422
    assert await _exists(feat["id"])


async def test_delete_scope_unknown_returns_404(client, actor):
    assert (await _delete(client, "scopes", str(uuid.uuid4()), actor)).status_code == 404

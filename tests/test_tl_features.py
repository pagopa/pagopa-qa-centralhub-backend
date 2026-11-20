from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select

from app.deps import get_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.test_lifecycle import TlScope
from app.models.user import User
from app.schemas.test_lifecycle import TlScenarioCreate
from app.services.test_lifecycle import features as features_svc
from tests._db import TestSession, get_test_db

BASE = "/api/v1/test-lifecycle"


@pytest.fixture(autouse=True)
def _use_test_db():
    app.dependency_overrides[get_db] = get_test_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
async def actor():
    email = f"tl-{uuid.uuid4().hex[:10]}@example.test"
    async with TestSession() as s:
        user = User(email=email, name="TL Tester", role="qa_manager", is_active=True)
        s.add(user)
        await s.commit()
        user_id = user.id
    yield {"id": str(user_id), "email": email}
    async with TestSession() as s:
        await s.execute(delete(AuditLog).where(AuditLog.actor_id == user_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


@pytest.fixture
async def scope(client, actor):
    code = "T" + uuid.uuid4().hex[:8]
    resp = await client.post(f"{BASE}/scopes", json={"code": code, "name": "Ambito"})
    assert resp.status_code == 201
    data = resp.json()
    yield data
    async with TestSession() as s:
        await s.execute(
            delete(AuditLog).where(
                AuditLog.resource_type == "tl_feature",
                AuditLog.resource_id.in_(
                    [r for r in await _feature_ids(s, uuid.UUID(data["id"]))]
                ),
            )
        )
        await s.execute(delete(TlScope).where(TlScope.id == uuid.UUID(data["id"])))
        await s.commit()


async def _feature_ids(session, scope_id):
    from app.models.test_lifecycle import TlFeature

    rows = await session.execute(select(TlFeature.id).where(TlFeature.scope_id == scope_id))
    return [str(r) for r in rows.scalars()]


async def _new_feature(client, scope, actor, title="Pagamento", **extra):
    resp = await client.post(
        f"{BASE}/features",
        json={"scope_id": scope["id"], "title": title, "actor_email": actor["email"], **extra},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _new_scenario(client, feat, actor, title="Happy path"):
    rev = feat["working_revision"]["id"]
    resp = await client.post(
        f"{BASE}/features/{feat['id']}/revisions/{rev}/scenarios",
        json={"actor_email": actor["email"], "title": title},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_feature_assigns_code_and_draft_revision(client, scope, actor):
    feat = await _new_feature(client, scope, actor, design_owner_id=actor["id"])
    assert feat["code"] == f"FEAT_001_{scope['code']}"
    assert feat["status"] == "draft"
    assert feat["design_owner_id"] == actor["id"]
    rev = feat["working_revision"]
    assert rev["number"] == 1
    assert rev["status"] == "draft"
    assert rev["title"] == "Pagamento"
    assert rev["lock_version"] == 0
    assert feat["published_revision"] is None
    assert feat["transitions"] == []
    async with TestSession() as s:
        logs = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.resource_id == feat["id"], AuditLog.action == "tl.feature.created"
                )
            )
        ).scalars().all()
    assert len(logs) == 1


async def test_second_feature_gets_next_number(client, scope, actor):
    await _new_feature(client, scope, actor)
    second = await _new_feature(client, scope, actor, title="Altro")
    assert second["code"] == f"FEAT_002_{scope['code']}"
    assert second["number"] == 2


async def test_unknown_actor_returns_403(client, scope):
    resp = await client.post(
        f"{BASE}/features",
        json={"scope_id": scope["id"], "title": "X", "actor_email": "nobody@example.test"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "unknown_actor"


async def test_update_revision_with_stale_lock_version_returns_409(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    url = f"{BASE}/features/{feat['id']}/revisions/{feat['working_revision']['id']}"
    ok = await client.patch(
        url, json={"lock_version": 0, "actor_email": actor["email"], "description": "uno"}
    )
    assert ok.status_code == 200
    assert ok.json()["lock_version"] == 1
    stale = await client.patch(
        url, json={"lock_version": 0, "actor_email": actor["email"], "description": "due"}
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_lock"


async def test_revision_not_draft_is_locked(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    rev_id = feat["working_revision"]["id"]
    from app.models.test_lifecycle import TlFeatureRevision, TlRevisionStatus

    async with TestSession() as s:
        rev = await s.get(TlFeatureRevision, uuid.UUID(rev_id))
        rev.status = TlRevisionStatus.IN_REVIEW
        await s.commit()
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev_id}",
        json={"lock_version": 0, "actor_email": actor["email"], "description": "x"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "code": "revision_locked",
        "message": "La revisione non è modificabile nello stato in_review",
    }


async def test_revision_of_other_feature_returns_404(client, scope, actor):
    a = await _new_feature(client, scope, actor)
    b = await _new_feature(client, scope, actor, title="B")
    resp = await client.patch(
        f"{BASE}/features/{a['id']}/revisions/{b['working_revision']['id']}",
        json={"lock_version": 0, "actor_email": actor["email"], "description": "x"},
    )
    assert resp.status_code == 404


async def test_add_scenario_assigns_identity_tag(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    sc = await _new_scenario(client, feat, actor)
    assert sc["tag"] == f"@{scope['code'].upper()}_001_01"
    assert sc["number"] == 1
    assert sc["position"] == 1


async def test_removed_scenario_number_is_never_reused(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    rev = feat["working_revision"]["id"]
    await _new_scenario(client, feat, actor, "uno")
    second = await _new_scenario(client, feat, actor, "due")
    resp = await client.delete(
        f"{BASE}/features/{feat['id']}/revisions/{rev}/scenarios/{second['id']}",
        params={"actor_email": actor["email"]},
    )
    assert resp.status_code == 200
    assert [s["number"] for s in resp.json()["scenarios"]] == [1]
    third = await _new_scenario(client, feat, actor, "tre")
    assert third["number"] == 3
    assert third["tag"].endswith("_001_03")
    assert third["position"] == 2


async def test_concurrent_scenario_creation_allocates_distinct_numbers(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    feature_id = uuid.UUID(feat["id"])
    rev_id = uuid.UUID(feat["working_revision"]["id"])

    async def add(title: str):
        async with TestSession() as s:
            return await features_svc.add_scenario(
                s, feature_id, rev_id, TlScenarioCreate(actor_email=actor["email"], title=title)
            )

    a, b = await asyncio.gather(add("A"), add("B"))
    assert {a.number, b.number} == {1, 2}
    assert {a.position, b.position} == {1, 2}


async def test_invalid_steps_are_saved_with_errors(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    rev = feat["working_revision"]["id"]
    sc = await _new_scenario(client, feat, actor)
    url = f"{BASE}/features/{feat['id']}/revisions/{rev}/scenarios/{sc['id']}"
    resp = await client.patch(
        url,
        json={
            "actor_email": actor["email"],
            "lock_version": 1,
            "steps": "Dato che qualcosa\nblabla senza keyword",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["steps"].startswith("Dato che")
    assert body["errors"] and body["errors"][0]["line"] == 2
    detail = (await client.get(f"{BASE}/features/{feat['id']}")).json()
    saved = detail["working_revision"]["scenarios"][0]
    assert saved["steps"].endswith("senza keyword")
    assert saved["errors"]


async def test_jira_keys_format_validated(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    url = f"{BASE}/features/{feat['id']}"
    bad = await client.patch(url, json={"jira_design_key": "ABC-1", "actor_email": actor["email"]})
    assert bad.status_code == 422
    ok = await client.patch(url, json={"jira_design_key": "PQ-12", "actor_email": actor["email"]})
    assert ok.status_code == 200
    assert ok.json()["jira_design_key"] == "PQ-12"


async def test_list_features_filters_by_scope_and_status(client, scope, actor):
    other = (
        await client.post(
            f"{BASE}/scopes", json={"code": "O" + uuid.uuid4().hex[:8], "name": "Altro"}
        )
    ).json()
    try:
        feat = await _new_feature(client, scope, actor, title="Nel primo")
        await _new_scenario(client, feat, actor)
        await _new_feature(client, other, actor, title="Nel secondo")
        by_scope = (await client.get(f"{BASE}/features", params={"scope_id": scope["id"]})).json()
        assert [f["id"] for f in by_scope] == [feat["id"]]
        assert by_scope[0]["title"] == "Nel primo"
        assert by_scope[0]["scenario_count"] == 1
        assert by_scope[0]["scope_code"] == scope["code"]
        drafts = (
            await client.get(
                f"{BASE}/features", params={"scope_id": scope["id"], "status": "draft"}
            )
        ).json()
        assert len(drafts) == 1
        none = (
            await client.get(
                f"{BASE}/features", params={"scope_id": scope["id"], "status": "designed"}
            )
        ).json()
        assert none == []
    finally:
        async with TestSession() as s:
            ids = await _feature_ids(s, uuid.UUID(other["id"]))
            await s.execute(delete(AuditLog).where(AuditLog.resource_id.in_(ids)))
            await s.execute(delete(TlScope).where(TlScope.id == uuid.UUID(other["id"])))
            await s.commit()


async def test_scenario_reorder_keeps_positions_contiguous(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    rev = feat["working_revision"]["id"]
    s1 = await _new_scenario(client, feat, actor, "uno")
    await _new_scenario(client, feat, actor, "due")
    await _new_scenario(client, feat, actor, "tre")
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev}/scenarios/{s1['id']}",
        json={"actor_email": actor["email"], "lock_version": 3, "position": 3},
    )
    assert resp.status_code == 200
    scenarios = (await client.get(f"{BASE}/features/{feat['id']}")).json()["working_revision"][
        "scenarios"
    ]
    assert [(s["title"], s["position"]) for s in scenarios] == [("due", 1), ("tre", 2), ("uno", 3)]


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "due\nrighe"},
        {"title": "   "},
        {"title": "x" * 256},
    ],
)
async def test_feature_title_hygiene(client, scope, actor, payload):
    resp = await client.post(
        f"{BASE}/features",
        json={"scope_id": scope["id"], "actor_email": actor["email"], **payload},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"description": "ok\n# commento"},
        {"description": "  @tag"},
        {"description": "Scenario: finto"},
        {"description": "Schema dello scenario: finto"},
    ],
)
async def test_revision_description_hygiene(client, scope, actor, payload):
    feat = await _new_feature(client, scope, actor)
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{feat['working_revision']['id']}",
        json={"lock_version": 0, "actor_email": actor["email"], **payload},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"extra_tags": ["@ui"]},
        {"extra_tags": ["boh"]},
        {"examples": {"headers": [], "rows": []}},
        {"examples": {"headers": ["a", "a"], "rows": []}},
        {"examples": {"headers": ["a", "b"], "rows": [["1"]]}},
    ],
)
async def test_scenario_update_input_hygiene(client, scope, actor, payload):
    feat = await _new_feature(client, scope, actor)
    rev = feat["working_revision"]["id"]
    sc = await _new_scenario(client, feat, actor)
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev}/scenarios/{sc['id']}",
        json={"actor_email": actor["email"], "lock_version": 1, **payload},
    )
    assert resp.status_code == 422


async def test_extra_tags_deduplicated(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    rev = feat["working_revision"]["id"]
    sc = await _new_scenario(client, feat, actor)
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev}/scenarios/{sc['id']}",
        json={"actor_email": actor["email"], "lock_version": 1, "extra_tags": ["ui", "ui", "e2e"]},
    )
    assert resp.status_code == 200
    assert resp.json()["extra_tags"] == ["ui", "e2e"]


async def test_unknown_feature_returns_404(client):
    resp = await client.get(f"{BASE}/features/{uuid.uuid4()}")
    assert resp.status_code == 404

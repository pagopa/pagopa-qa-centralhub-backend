# ruff: noqa: S101, F811
"""Fase 5, Task 8: API della migrazione."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.models.audit_log import AuditLog
from app.models.test_lifecycle import TlMigrationItem
from app.models.user import User
from tests._db import TestSession
from tests.test_tl_features import BASE, _use_test_db, actor  # noqa: F401
from tests.test_tl_migration_execute import env  # noqa: F401
from tests.tl_fakes import install_fake_confluence, install_fake_git
from tests.tl_migration_fixtures import clean_migration  # noqa: F401

M = f"{BASE}/migration"


@pytest.fixture
def wired(env, monkeypatch):
    s, conf, git, actor = env
    install_fake_confluence(monkeypatch, conf)
    install_fake_git(monkeypatch, git)
    return env


@pytest.fixture
async def guest():
    email = f"guest-{uuid.uuid4().hex[:8]}@example.test"
    async with TestSession() as s:
        user = User(email=email, name="Ospite", role="guest", is_active=True)
        s.add(user)
        await s.commit()
        uid = user.id
    yield email
    async with TestSession() as s:
        await s.execute(delete(AuditLog).where(AuditLog.actor_id == uid))
        await s.execute(delete(User).where(User.id == uid))
        await s.commit()


async def test_analyze_list_patch_and_execute(client, wired):
    s, conf, git, actor = wired
    who = {"actor_email": actor["email"]}
    resp = await client.post(f"{M}/analyze", json=who)
    assert resp.status_code == 200, resp.text
    assert {sc["scope_code"] for sc in resp.json()["scopes"]} >= {s}
    assert resp.json()["confluence_write"] is False

    items = (await client.get(f"{M}/items", params={**who, "scope_code": s})).json()
    assert {i["page_id"] for i in items} == {"p1", "p2"}
    p2 = next(i for i in items if i["page_id"] == "p2")
    bad = await client.patch(f"{M}/items/{p2['id']}", json={**who, "overrides": {"nope": 1}})
    assert bad.status_code == 422
    ok = await client.patch(
        f"{M}/items/{p2['id']}",
        json={**who, "decision": "confirmed", "overrides": {"title": "Rimborso totale"}},
    )
    assert ok.status_code == 200 and ok.json()["effective"]["title"] == "Rimborso totale"
    filtered = (
        await client.get(f"{M}/items", params={**who, "problem": "page_without_file"})
    ).json()
    assert [i["page_id"] for i in filtered] == ["p2"]

    tag = await client.patch(f"{M}/tags/regression", json={**who, "decision": "drop"})
    assert tag.status_code == 200
    assert next(t for t in tag.json()["tags"] if t["name"] == "regression")["decision"] == "drop"

    done = await client.post(f"{M}/scopes/{s}/execute", json=who)
    assert done.status_code == 200, done.text
    assert done.json()["executed"] == 1
    executed = await client.patch(f"{M}/items/{p2['id']}", json={**who, "decision": "excluded"})
    assert executed.status_code == 409
    assert executed.json()["detail"]["code"] == "migration_item_executed"

    retry = await client.post(f"{M}/scopes/{s}/retry-confluence", json=who)
    assert retry.status_code == 200 and retry.json()["confluence_errors"]
    retry_pr = await client.post(f"{M}/scopes/{s}/retry-pr", json=who)
    assert retry_pr.status_code == 200


async def test_blocked_execution_returns_reasons(client, wired):
    s, conf, git, actor = wired
    who = {"actor_email": actor["email"]}
    await client.post(f"{M}/analyze", json=who)
    async with TestSession() as db:
        rows = (
            await db.execute(select(TlMigrationItem).where(TlMigrationItem.scope_code == s))
        ).scalars()
        for r in rows:
            r.decision, r.overrides = "confirmed", {"number": 4}
        await db.commit()
    resp = await client.post(f"{M}/scopes/{s}/execute", json=who)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "migration_blocked"
    assert resp.json()["detail"]["reasons"]


async def test_permission_and_configuration(client, clean_migration, actor, guest):
    resp = await client.get(M, params={"actor_email": guest})
    assert resp.status_code == 403
    resp = await client.post(f"{M}/analyze", json={"actor_email": actor["email"]})
    assert resp.status_code == 503
    resp = await client.get(M, params={"actor_email": actor["email"]})
    assert resp.status_code == 200 and resp.json()["status"] == "idle"


async def test_feature_detail_exposes_migration_fields(client, wired):
    s, conf, git, actor = wired
    who = {"actor_email": actor["email"]}
    await client.post(f"{M}/analyze", json=who)
    async with TestSession() as db:
        rows = (
            await db.execute(select(TlMigrationItem).where(TlMigrationItem.scope_code == s))
        ).scalars()
        for r in rows:
            r.decision = "confirmed"
        await db.commit()
    await client.post(f"{M}/scopes/{s}/execute", json=who)
    async with TestSession() as db:
        fid = (
            await db.execute(
                select(TlMigrationItem.feature_id).where(TlMigrationItem.page_id == "p1")
            )
        ).scalar_one()
    detail = (await client.get(f"{BASE}/features/{fid}")).json()
    assert detail["alignment_pending"] is True
    assert detail["migrated_from"]["page_id"] == "p1"
    upd = await client.patch(
        f"{BASE}/features/{fid}", json={**who, "migration_note": "Controllata"}
    )
    assert upd.status_code == 200 and upd.json()["migration_note"] == "Controllata"

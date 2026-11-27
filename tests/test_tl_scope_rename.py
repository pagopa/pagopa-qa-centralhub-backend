# ruff: noqa: F811, S101
"""Renaming a scope's code adapts its features (design 2026-11-27)."""
from __future__ import annotations

import uuid

from sqlalchemy import delete, select, update

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureStatus,
    TlRevisionStatus,
    TlScenarioAlias,
    TlScope,
)
from app.services.test_lifecycle.advisory_lock import try_advisory_lock
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _new_scenario,
    _use_test_db,
    actor,
    scope,
)
from tests.test_tl_git_import import _events


def _code() -> str:
    return "R" + uuid.uuid4().hex[:8]


async def _feature(client, scope, actor, scenarios=2):
    feat = await _new_feature(client, scope, actor)
    for i in range(scenarios):
        await _new_scenario(client, feat, actor, title=f"S{i}")
    return (await client.get(f"{BASE}/features/{feat['id']}")).json()


async def _rename(client, scope_id, **body):
    return await client.patch(f"{BASE}/scopes/{scope_id}", json=body)


async def _block(fid):
    async with TestSession() as s:
        feature = await s.get(TlFeature, uuid.UUID(fid))
        rid = feature.working_revision_id
        await s.execute(
            update(TlFeatureRevision).where(TlFeatureRevision.id == rid)
            .values(status=TlRevisionStatus.MERGED)
        )
        await s.execute(
            update(TlFeature).where(TlFeature.id == feature.id).values(
                published_revision_id=rid, working_revision_id=None, alignment_pending=False,
                git_path="src/x/a.feature", status=TlFeatureStatus.IMPLEMENTED,
            )
        )
        await s.commit()


async def test_renaming_the_code_adapts_every_feature(client, scope, actor):
    a = await _feature(client, scope, actor)
    b = await _feature(client, scope, actor)
    old_tags = [s["tag"] for s in a["working_revision"]["scenarios"]]
    new = _code()
    resp = await _rename(client, scope["id"], code=new)
    assert resp.status_code == 200, resp.text
    assert resp.json()["code"] == new
    for before in (a, b):
        after = (await client.get(f"{BASE}/features/{before['id']}")).json()
        assert after["code"] == f"FEAT_{before['number']:03d}_{new}"
        assert after["number"] == before["number"] and after["scope_code"] == new
        tags = [s["tag"] for s in after["working_revision"]["scenarios"]]
        assert all(t.startswith(f"@{new.upper()}_{before['number']:03d}_") for t in tags)
    async with TestSession() as s:
        aliases = set((await s.execute(select(TlScenarioAlias.alias))).scalars())
    assert {t.lstrip("@") for t in old_tags} <= aliases
    events = await _events(a["id"], "tl.feature.moved")
    assert len(events) == 1 and events[0]["old_scope"] == scope["code"]
    assert events[0]["new_scope"] == new and events[0]["new_code"].endswith(new)


async def test_one_blocked_feature_refuses_the_rename_and_changes_nothing(client, scope, actor):
    good = await _feature(client, scope, actor)
    bad = await _feature(client, scope, actor)
    await _block(bad["id"])
    resp = await _rename(client, scope["id"], code=_code())
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "scope_code_blocked"
    assert [b["feature_id"] for b in detail["blocked"]] == [bad["id"]]
    assert detail["blocked"][0]["reasons"]
    scopes = (await client.get(f"{BASE}/scopes")).json()
    assert next(s for s in scopes if s["id"] == scope["id"])["code"] == scope["code"]
    after = (await client.get(f"{BASE}/features/{good['id']}")).json()
    assert after["code"] == good["code"]


async def test_renaming_only_the_name_touches_no_feature(client, scope, actor):
    feat = await _feature(client, scope, actor)
    resp = await _rename(client, scope["id"], name="Dismissione WISP")
    assert resp.status_code == 200 and resp.json()["name"] == "Dismissione WISP"
    after = (await client.get(f"{BASE}/features/{feat['id']}")).json()
    assert after["code"] == feat["code"]
    assert await _events(feat["id"], "tl.feature.moved") == []


async def test_a_code_used_by_another_scope_is_still_a_conflict(client, scope, actor):
    other = (
        await client.post(f"{BASE}/scopes", json={"code": _code(), "name": "Altro"})
    ).json()
    try:
        resp = await _rename(client, scope["id"], code=other["code"].lower())
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "scope_code_conflict"
    finally:
        async with TestSession() as s:
            await s.execute(delete(TlScope).where(TlScope.id == uuid.UUID(other["id"])))
            await s.commit()


async def test_a_running_migration_refuses_the_rename(client, scope, actor):
    await _feature(client, scope, actor)
    async with TestSession() as s, try_advisory_lock(s, "tl_migration") as got:
        assert got
        resp = await _rename(client, scope["id"], code=_code())
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "migration_in_progress"


async def test_a_new_feature_skips_a_code_already_taken_by_a_kept_one(client, actor):
    code = _code()
    resp = await client.post(f"{BASE}/scopes", json={"code": code, "name": "Nuovo"})
    new_scope = resp.json()
    holder = (await client.post(f"{BASE}/scopes", json={"code": _code(), "name": "Altro"})).json()
    try:
        async with TestSession() as s:  # a feature that kept its code when it was moved elsewhere
            s.add(
                TlFeature(
                    scope_id=uuid.UUID(holder["id"]), number=1, code=f"FEAT_001_{code}",
                    status=TlFeatureStatus.DRAFT, created_by="t@example.test",
                )
            )
            await s.commit()
        feat = await _new_feature(client, new_scope, actor)
        assert feat["code"] == f"FEAT_002_{code}" and feat["number"] == 2
    finally:
        async with TestSession() as s:
            await s.execute(
                delete(TlScope).where(
                    TlScope.id.in_([uuid.UUID(new_scope["id"]), uuid.UUID(holder["id"])])
                )
            )
            await s.commit()


async def test_a_code_rename_refreshes_the_alignment_prs(client, scope, actor, monkeypatch):
    from app.api.v1 import test_lifecycle as api

    calls = []

    async def fake_refresh(db, git, moves):
        calls.append(list(moves))
        return "PR di allineamento: GitHub giù"

    monkeypatch.setattr(api.migration_align, "refresh_after_change", fake_refresh)
    await _feature(client, scope, actor)
    new = _code()
    resp = await _rename(client, scope["id"], code=new)
    assert resp.status_code == 200, resp.text
    assert calls == [[(scope["code"], new)]]
    assert resp.json()["alignment_error"] == "PR di allineamento: GitHub giù"
    # a name-only change refreshes nothing
    calls.clear()
    assert (await _rename(client, scope["id"], name="Nome nuovo")).status_code == 200
    assert calls == []

# ruff: noqa: F811, S101
"""Moving features between scopes over HTTP (design 2026-11-27)."""
from __future__ import annotations

import uuid

import pytest
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

MOVE = f"{BASE}/features/move"


@pytest.fixture
async def target(client):
    code = "W" + uuid.uuid4().hex[:8]
    resp = await client.post(
        f"{BASE}/scopes", json={"code": code, "name": "Destinazione", "repo_folder": "src/x"}
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    yield data
    async with TestSession() as s:
        await s.execute(delete(TlScope).where(TlScope.id == uuid.UUID(data["id"])))
        await s.commit()


async def _feature(client, scope, actor, scenarios=2):
    feat = await _new_feature(client, scope, actor)
    for i in range(scenarios):
        await _new_scenario(client, feat, actor, title=f"S{i}")
    return (await client.get(f"{BASE}/features/{feat['id']}")).json()


async def _move(client, actor, ids, target_id, **extra):
    return await client.post(
        MOVE,
        json={
            "feature_ids": ids, "target_scope_id": target_id,
            "actor_email": actor["email"], **extra,
        },
    )


async def _scope(client, scope_id):
    scopes = (await client.get(f"{BASE}/scopes")).json()
    return next(s for s in scopes if s["id"] == scope_id)


async def _block_feature(fid):
    """Make the feature look published with hub identifiers on main."""
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


async def test_move_with_adapting_gives_the_new_identifiers(client, scope, actor, target):
    feat = await _feature(client, scope, actor)
    old_tags = [s["tag"] for s in feat["working_revision"]["scenarios"]]
    counter = (await _scope(client, target["id"]))["next_feature_number"]
    resp = await _move(client, actor, [feat["id"]], target["id"])
    assert resp.status_code == 200, resp.text
    (item,) = resp.json()["moved"]
    assert item["old_code"] == feat["code"]
    assert item["new_code"] == f"FEAT_{counter:03d}_{target['code']}"
    assert item["old_scope_code"] == scope["code"] and item["new_scope_code"] == target["code"]
    after = (await client.get(f"{BASE}/features/{feat['id']}")).json()
    assert after["code"] == item["new_code"] and after["scope_code"] == target["code"]
    new_tags = [s["tag"] for s in after["working_revision"]["scenarios"]]
    assert all(t.startswith(f"@{target['code'].upper()}_{counter:03d}_") for t in new_tags)
    async with TestSession() as s:
        aliases = set((await s.execute(select(TlScenarioAlias.alias))).scalars())
    assert {t.lstrip("@") for t in old_tags} <= aliases


async def test_a_bulk_move_numbers_the_features_consecutively(client, scope, actor, target):
    feats = [await _feature(client, scope, actor, scenarios=1) for _ in range(3)]
    counter = (await _scope(client, target["id"]))["next_feature_number"]
    resp = await _move(client, actor, [f["id"] for f in feats], target["id"])
    assert resp.status_code == 200, resp.text
    numbers = []
    for f in feats:
        numbers.append((await client.get(f"{BASE}/features/{f['id']}")).json()["number"])
    assert sorted(numbers) == [counter, counter + 1, counter + 2]
    assert (await _scope(client, target["id"]))["next_feature_number"] == counter + 3


async def test_a_feature_already_in_the_target_is_skipped(client, scope, actor):
    feat = await _feature(client, scope, actor)
    resp = await _move(client, actor, [feat["id"]], scope["id"])
    assert resp.status_code == 200
    assert resp.json()["moved"] == [] and resp.json()["skipped"] == [feat["id"]]


async def test_one_blocked_feature_stops_the_whole_request(client, scope, actor, target):
    good = await _feature(client, scope, actor)
    bad = await _feature(client, scope, actor)
    await _block_feature(bad["id"])
    counter = (await _scope(client, target["id"]))["next_feature_number"]
    before = (await client.get(f"{BASE}/features/{good['id']}")).json()
    resp = await _move(client, actor, [good["id"], bad["id"]], target["id"])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "adapt_blocked"
    assert [b["feature_id"] for b in detail["blocked"]] == [bad["id"]]
    assert detail["blocked"][0]["reasons"]
    after = (await client.get(f"{BASE}/features/{good['id']}")).json()
    assert (after["scope_id"], after["code"], after["number"]) == (
        before["scope_id"], before["code"], before["number"],
    )
    assert (await _scope(client, target["id"]))["next_feature_number"] == counter


async def test_unknown_feature_or_scope_is_404(client, scope, actor, target):
    feat = await _feature(client, scope, actor)
    unknown = "00000000-0000-0000-0000-000000000000"
    assert (await _move(client, actor, [feat["id"], unknown], target["id"])).status_code == 404
    assert (await _move(client, actor, [feat["id"]], unknown)).status_code == 404
    unchanged = (await client.get(f"{BASE}/features/{feat['id']}")).json()
    assert unchanged["scope_id"] == scope["id"]


async def test_unknown_actor_is_403_and_an_empty_request_is_422(client, scope, actor, target):
    feat = await _feature(client, scope, actor)
    resp = await client.post(
        MOVE,
        json={"feature_ids": [feat["id"]], "target_scope_id": target["id"],
              "actor_email": "nobody@example.test"},
    )
    assert resp.status_code == 403
    assert (await _move(client, actor, [], target["id"])).status_code == 422


async def test_a_running_migration_refuses_the_move(client, scope, actor, target):
    feat = await _feature(client, scope, actor)
    async with TestSession() as s, try_advisory_lock(s, "tl_migration") as got:
        assert got
        resp = await _move(client, actor, [feat["id"]], target["id"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "migration_in_progress"


async def test_each_moved_feature_gets_a_history_event(client, scope, actor, target):
    feat = await _feature(client, scope, actor)
    await _move(client, actor, [feat["id"]], target["id"])
    events = await _events(feat["id"], "tl.feature.moved")
    assert len(events) == 1
    assert events[0]["old_scope"] == scope["code"] and events[0]["new_scope"] == target["code"]
    assert events[0]["old_code"] == feat["code"] and events[0]["new_code"].endswith(target["code"])


# ── migration alignment pull requests are refreshed after the move ───────────────


@pytest.fixture
def refresh(monkeypatch):
    from app.api.v1 import test_lifecycle as api

    calls = []
    state = {"answer": None, "raise": False}

    async def fake_refresh(db, git, moves):
        calls.append(list(moves))
        if state["raise"]:
            raise RuntimeError("boom")
        return state["answer"]

    monkeypatch.setattr(api.migration_align, "refresh_after_change", fake_refresh)
    return calls, state


async def test_an_adapting_move_refreshes_the_alignment_prs(client, scope, actor, target, refresh):
    calls, state = refresh
    state["answer"] = "PR di allineamento X: GitHub giù"
    feat = await _feature(client, scope, actor)
    resp = await _move(client, actor, [feat["id"]], target["id"])
    assert resp.status_code == 200, resp.text
    assert calls == [[(scope["code"], target["code"])]]
    assert resp.json()["alignment_error"] == "PR di allineamento X: GitHub giù"


async def test_an_unexpected_refresh_failure_never_fails_the_move(
    client, scope, actor, target, refresh
):
    _, state = refresh
    state["raise"] = True
    feat = await _feature(client, scope, actor)
    resp = await _move(client, actor, [feat["id"]], target["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["alignment_error"]
    after = (await client.get(f"{BASE}/features/{feat['id']}")).json()
    assert after["scope_id"] == target["id"]


async def test_a_move_skips_a_code_already_held_by_another_feature(client, scope, actor, target):
    feat = await _feature(client, scope, actor)
    counter = (await _scope(client, target["id"]))["next_feature_number"]
    other = (await client.post(f"{BASE}/scopes", json={"code": "O" + uuid.uuid4().hex[:8],
                                                       "name": "Altro"})).json()
    try:
        async with TestSession() as s:
            s.add(
                TlFeature(
                    scope_id=uuid.UUID(other["id"]), number=1,
                    code=f"FEAT_{counter:03d}_{target['code']}",
                    status=TlFeatureStatus.DRAFT, created_by="t@example.test",
                )
            )
            await s.commit()
        resp = await _move(client, actor, [feat["id"]], target["id"])
        assert resp.status_code == 200, resp.text
        (item,) = resp.json()["moved"]
        assert item["new_code"] == f"FEAT_{counter + 1:03d}_{target['code']}"
    finally:
        async with TestSession() as s:
            await s.execute(delete(TlScope).where(TlScope.id == uuid.UUID(other["id"])))
            await s.commit()

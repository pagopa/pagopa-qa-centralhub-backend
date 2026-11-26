# ruff: noqa: S101, F811
"""Cleanup of test-type tags carried by scenarios (design 2026-11-26, tag catalog)."""
from __future__ import annotations

import pytest

from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _new_scenario,
    _use_test_db,
    actor,
    scope,
)
from tests.test_tl_workflow import _do, _get, _ready_feature, reviewer  # noqa: F401


async def _tag(client, feat, actor, scenario, extra):
    detail = await _get(client, feat["id"])
    rev = detail["working_revision"]
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev['id']}/scenarios/{scenario['id']}",
        json={
            "actor_email": actor["email"],
            "lock_version": rev["lock_version"],
            "extra_tags": extra,
        },
    )
    assert resp.status_code == 200, resp.text


async def _feature_with(client, scope, actor, per_scenario, **extra):
    feat = await _new_feature(client, scope, actor, **extra)
    scenarios = []
    for i, tags_ in enumerate(per_scenario):
        sc = await _new_scenario(client, feat, actor, title=f"S{i}")
        await _tag(client, feat, actor, sc, tags_)
        scenarios.append(sc)
    return feat, scenarios


async def _get_cleanup(client, feat):
    resp = await client.get(f"{BASE}/features/{feat['id']}/tag-cleanup")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _apply(client, feat, actor):
    return await client.post(
        f"{BASE}/features/{feat['id']}/tag-cleanup", json={"actor_email": actor["email"]}
    )


async def test_lists_only_scenarios_with_test_type_tags(client, scope, actor):
    feat, (a, b, c) = await _feature_with(
        client, scope, actor, [["runnable", "e2e"], ["runnable", "ui"], ["skip", "e2e", "smoke"]]
    )
    out = await _get_cleanup(client, feat)
    assert [(r["scenario_tag"], r["tags"]) for r in out["rows"]] == [
        (a["tag"], ["e2e"]),
        (c["tag"], ["e2e"]),
    ]
    assert out["proposed_test_type"] == "e2e" and out["conflict"] is None


async def test_nothing_to_clean(client, scope, actor):
    feat, _ = await _feature_with(client, scope, actor, [["runnable", "ui"]])
    assert await _get_cleanup(client, feat) == {
        "rows": [], "proposed_test_type": None, "conflict": None,
    }


async def test_same_type_as_the_file_is_not_a_conflict(client, scope, actor):
    feat, _ = await _feature_with(client, scope, actor, [["runnable", "api"]], test_type="api")
    out = await _get_cleanup(client, feat)
    assert out["proposed_test_type"] == "api" and out["conflict"] is None


async def test_scenarios_disagreeing_among_themselves_are_a_conflict(client, scope, actor):
    feat, _ = await _feature_with(client, scope, actor, [["runnable", "e2e"], ["runnable", "api"]])
    out = await _get_cleanup(client, feat)
    assert out["proposed_test_type"] is None and out["conflict"]


async def test_disagreeing_with_the_file_is_a_conflict(client, scope, actor):
    feat, _ = await _feature_with(
        client, scope, actor, [["runnable", "e2e"]], test_type="integration"
    )
    out = await _get_cleanup(client, feat)
    assert out["proposed_test_type"] is None and "integration" in out["conflict"]


async def test_apply_removes_the_tags_and_sets_the_file_test_type(client, scope, actor):
    feat, (a, b) = await _feature_with(
        client, scope, actor, [["runnable", "e2e", "smoke"], ["skip", "ui"]]
    )
    before = (await _get(client, feat["id"]))["working_revision"]["lock_version"]
    resp = await _apply(client, feat, actor)
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["test_type"] == "e2e"
    tags = {s["tag"]: s["extra_tags"] for s in detail["working_revision"]["scenarios"]}
    assert tags == {a["tag"]: ["runnable", "smoke"], b["tag"]: ["skip", "ui"]}
    assert detail["working_revision"]["lock_version"] == before + 1
    assert (await _get_cleanup(client, feat))["rows"] == []


async def test_apply_keeps_a_matching_file_test_type(client, scope, actor):
    feat, _ = await _feature_with(client, scope, actor, [["runnable", "api"]], test_type="api")
    resp = await _apply(client, feat, actor)
    assert resp.status_code == 200 and resp.json()["test_type"] == "api"


async def test_apply_with_a_conflict_is_409_and_changes_nothing(client, scope, actor):
    feat, (a,) = await _feature_with(
        client, scope, actor, [["runnable", "e2e"]], test_type="integration"
    )
    resp = await _apply(client, feat, actor)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "tag_cleanup_conflict"
    detail = await _get(client, feat["id"])
    assert detail["test_type"] == "integration"
    assert detail["working_revision"]["scenarios"][0]["extra_tags"] == ["runnable", "e2e"]


async def test_apply_on_a_revision_that_is_not_a_draft_is_409(client, scope, actor, reviewer):
    feat = await _ready_feature(client, scope, actor, reviewer)
    sc = feat["working_revision"]["scenarios"][0]
    await _tag(client, feat, actor, sc, ["runnable", "e2e"])
    detail = await _get(client, feat["id"], actor["email"])
    assert (await _do(client, detail, "submit", actor["email"])).status_code == 200
    resp = await _apply(client, feat, actor)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "revision_locked"


async def test_apply_by_an_unknown_actor_is_403(client, scope, actor):
    feat, _ = await _feature_with(client, scope, actor, [["runnable", "e2e"]])
    resp = await client.post(
        f"{BASE}/features/{feat['id']}/tag-cleanup", json={"actor_email": "nobody@example.test"}
    )
    assert resp.status_code == 403


async def test_unknown_feature_is_404(client):
    resp = await client.get(f"{BASE}/features/00000000-0000-0000-0000-000000000000/tag-cleanup")
    assert resp.status_code == 404


@pytest.mark.parametrize("method", ["get", "post"])
async def test_the_routes_exist(client, method):
    path = f"{BASE}/features/00000000-0000-0000-0000-000000000000/tag-cleanup"
    kwargs = {"json": {"actor_email": "x"}} if method == "post" else {}
    resp = await getattr(client, method)(path, **kwargs)
    assert resp.status_code != 405

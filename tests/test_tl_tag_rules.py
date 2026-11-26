# ruff: noqa: S101, F811
"""Tag standard: feature test type, scenario tag rules, submit rules (design 2026-11-26)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.models.test_lifecycle import TlCategoryTag, TlScope
from app.services.test_lifecycle import tags
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _new_scenario,
    _use_test_db,
    actor,
    scope,
)


async def _patch_scenario(client, feat, sc, actor, payload, lock):
    rev = feat["working_revision"]["id"]
    return await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev}/scenarios/{sc['id']}",
        json={"actor_email": actor["email"], "lock_version": lock, **payload},
    )


async def _scope_in(client, folder):
    resp = await client.post(
        f"{BASE}/scopes",
        json={"code": "R" + uuid.uuid4().hex[:8], "name": "Ambito", "repo_folder": folder},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _drop_scope(scope_id):
    async with TestSession() as s:
        await s.execute(delete(TlScope).where(TlScope.id == uuid.UUID(scope_id)))
        await s.commit()


# ── feature test type ────────────────────────────────────────────────────────


async def test_create_takes_test_type_from_the_scope_folder(client, actor):
    sc = await _scope_in(client, "src/e2e/pos")
    try:
        feat = await _new_feature(client, sc, actor)
        assert feat["test_type"] == "e2e"
    finally:
        await _drop_scope(sc["id"])


async def test_create_without_matching_folder_leaves_test_type_empty(client, scope, actor):
    feat = await _new_feature(client, scope, actor)  # folder "features/ambito"
    assert feat["test_type"] is None


async def test_create_with_explicit_test_type(client, scope, actor):
    feat = await _new_feature(client, scope, actor, test_type="api")
    assert feat["test_type"] == "api"


async def test_create_with_invalid_test_type_is_422(client, scope, actor):
    resp = await client.post(
        f"{BASE}/features",
        json={"scope_id": scope["id"], "title": "X", "actor_email": actor["email"],
              "test_type": "smoke"},
    )
    assert resp.status_code == 422


async def test_update_sets_and_clears_test_type_on_a_draft(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}",
        json={"actor_email": actor["email"], "test_type": "integration"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["test_type"] == "integration"
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}",
        json={"actor_email": actor["email"], "test_type": None},
    )
    assert resp.json()["test_type"] is None


async def test_update_with_invalid_test_type_is_422(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}",
        json={"actor_email": actor["email"], "test_type": "ui"},
    )
    assert resp.status_code == 422


async def test_list_exposes_test_type(client, scope, actor):
    feat = await _new_feature(client, scope, actor, test_type="api")
    rows = (await client.get(f"{BASE}/features", params={"scope_id": scope["id"]})).json()
    items = rows["items"] if isinstance(rows, dict) else rows
    assert next(r for r in items if r["id"] == feat["id"])["test_type"] == "api"


# ── scenario tag rules at save time ──────────────────────────────────────────


async def test_new_scenario_starts_runnable(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    sc = await _new_scenario(client, feat, actor)
    assert sc["extra_tags"] == ["runnable"]
    assert sc["polarity"] == "positivo"


@pytest.mark.parametrize(
    "extra",
    [
        ["skip", "anything_custom"],
        [],  # no status tag: saving is never blocked, only the submit is
        ["runnable", "skip"],
        ["e2e"],  # legacy test-type tag on a scenario still saves until the cleanup
        ["flaky", "runnable", "smoke-test_1"],
    ],
)
async def test_scenario_save_accepts_any_well_formed_combination(client, scope, actor, extra):
    feat = await _new_feature(client, scope, actor)
    sc = await _new_scenario(client, feat, actor)
    resp = await _patch_scenario(client, feat, sc, actor, {"extra_tags": extra}, lock=1)
    assert resp.status_code == 200, resp.text
    assert resp.json()["extra_tags"] == extra


@pytest.mark.parametrize(
    "extra",
    [
        ["positive"], ["negative"], ["positivo"], ["negativo"], ["POS_006_01"], ["A_001"],
        ["@ui"], ["9bad"], ["has space"],
    ],
)
async def test_scenario_save_rejects_polarity_identity_and_malformed(
    client, scope, actor, extra
):
    feat = await _new_feature(client, scope, actor)
    sc = await _new_scenario(client, feat, actor)
    resp = await _patch_scenario(client, feat, sc, actor, {"extra_tags": extra}, lock=1)
    assert resp.status_code == 422


# ── suggestions ──────────────────────────────────────────────────────────────


async def test_category_tags_endpoint_includes_tags_in_use(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    sc = await _new_scenario(client, feat, actor)
    used = f"u{uuid.uuid4().hex[:8]}"
    resp = await _patch_scenario(client, feat, sc, actor, {"extra_tags": ["runnable", used]}, 1)
    assert resp.status_code == 200
    listed = (await client.get(f"{BASE}/category-tags")).json()
    assert used in listed
    assert listed == sorted(listed)


async def test_category_tags_endpoint_keeps_table_entries():
    name = f"t{uuid.uuid4().hex[:8]}"
    async with TestSession() as s:
        s.add(TlCategoryTag(name=name))
        await s.commit()
    try:
        from httpx import ASGITransport, AsyncClient

        from app.main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assert name in (await c.get(f"{BASE}/category-tags")).json()
    finally:
        async with TestSession() as s:
            await s.execute(delete(TlCategoryTag).where(TlCategoryTag.name == name))
            await s.commit()


# ── reserved vocabulary is exposed for the rules, not the catalog ────────────


def test_reserved_names_are_not_status_free_form():
    assert set(tags.STATUS_TAGS) | {tags.FLAKY_TAG} | set(tags.POLARITY_TAGS) | set(
        tags.TEST_TYPES
    ) >= {"runnable", "skip", "broken", "flaky", "positive", "negative", "integration"}


# ── submit rules ─────────────────────────────────────────────────────────────

from tests.test_tl_workflow import (  # noqa: E402, F401
    _do,
    _get,
    _ready_feature,
    _transition,
    reviewer,
)


async def _set_test_type(client, feat, actor, value):
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}",
        json={"actor_email": actor["email"], "test_type": value},
    )
    assert resp.status_code == 200, resp.text


async def test_submit_blocked_without_test_type(client, scope, actor, reviewer):
    feat = await _ready_feature(client, scope, actor, reviewer)
    await _set_test_type(client, feat, actor, None)
    detail = await _get(client, feat["id"], actor["email"])
    submit = _transition(detail, "submit")
    assert submit["allowed"] is False
    assert tags.TEST_TYPE_MISSING_REASON in submit["reasons"]
    await _set_test_type(client, feat, actor, "integration")
    detail = await _get(client, feat["id"], actor["email"])
    submit = _transition(detail, "submit")
    assert tags.TEST_TYPE_MISSING_REASON not in submit["reasons"]
    assert submit["allowed"] is True, submit["reasons"]


@pytest.mark.parametrize("extra", [[], ["runnable", "skip"], ["smoke"]])
async def test_submit_blocked_when_scenario_status_is_not_exactly_one(
    client, scope, actor, reviewer, extra
):
    feat = await _ready_feature(client, scope, actor, reviewer)
    rev = feat["working_revision"]
    sc = rev["scenarios"][0]
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev['id']}/scenarios/{sc['id']}",
        json={
            "actor_email": actor["email"],
            "lock_version": (await _get(client, feat["id"]))["working_revision"]["lock_version"],
            "extra_tags": extra,
        },
    )
    assert resp.status_code == 200, resp.text
    submit = _transition(await _get(client, feat["id"], actor["email"]), "submit")
    assert submit["allowed"] is False
    assert tags.status_reason(sc["tag"]) in submit["reasons"]


async def test_submit_allowed_with_exactly_one_status_and_custom_tags(
    client, scope, actor, reviewer
):
    feat = await _ready_feature(client, scope, actor, reviewer)
    rev = feat["working_revision"]
    sc = rev["scenarios"][0]
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev['id']}/scenarios/{sc['id']}",
        json={
            "actor_email": actor["email"],
            "lock_version": (await _get(client, feat["id"]))["working_revision"]["lock_version"],
            "extra_tags": ["broken", "flaky", "checkout"],
        },
    )
    assert resp.status_code == 200, resp.text
    submit = _transition(await _get(client, feat["id"], actor["email"]), "submit")
    assert submit["allowed"] is True, submit["reasons"]

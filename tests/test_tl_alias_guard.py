# ruff: noqa: F811, S101
"""Historical tags (aliases) keep their scope code and numbers from being reused."""
from __future__ import annotations

import uuid

from sqlalchemy import delete, select

from app.models.test_lifecycle import TlScenario, TlScenarioAlias, TlScope
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _new_scenario,
    _use_test_db,
    actor,
    scope,
)
from tests.test_tl_move import MOVE, _feature, _scope, target  # noqa: F401


def _code() -> str:
    return "G" + uuid.uuid4().hex[:8]


async def _cleanup(*scope_ids):
    async with TestSession() as s:
        await s.execute(delete(TlScope).where(TlScope.id.in_([uuid.UUID(i) for i in scope_ids])))
        await s.commit()


async def _moved_away(client, scope, actor, target):
    """A feature leaves `scope` (its code now lives only as alias of the moved scenarios)."""
    feat = await _feature(client, scope, actor)
    resp = await client.post(
        MOVE,
        json={"feature_ids": [feat["id"]], "target_scope_id": target["id"],
              "actor_email": actor["email"]},
    )
    assert resp.status_code == 200, resp.text
    renamed = await client.patch(f"{BASE}/scopes/{scope['id']}", json={"code": _code()})
    assert renamed.status_code == 200, renamed.text


async def test_a_code_held_by_aliases_cannot_open_a_new_scope(client, scope, actor, target):
    await _moved_away(client, scope, actor, target)
    resp = await client.post(
        f"{BASE}/scopes", json={"code": scope["code"], "name": "Riuso", "repo_folder": "src/y"}
    )
    assert resp.status_code == 409, resp.text


async def test_a_code_held_by_aliases_cannot_be_a_rename_target(client, scope, actor, target):
    await _moved_away(client, scope, actor, target)
    other = (await client.post(f"{BASE}/scopes", json={"code": _code(), "name": "Altro"})).json()
    try:
        resp = await client.patch(f"{BASE}/scopes/{other['id']}", json={"code": scope["code"]})
        assert resp.status_code == 409, resp.text
    finally:
        await _cleanup(other["id"])


async def test_a_new_feature_skips_a_number_whose_tags_survive_as_aliases(
    client, scope, actor, target
):
    feat = await _feature(client, scope, actor)
    counter = (await _scope(client, target["id"]))["next_feature_number"]
    async with TestSession() as s:
        scenario = (
            await s.execute(
                select(TlScenario).where(TlScenario.feature_id == uuid.UUID(feat["id"]))
            )
        ).scalars().first()
        s.add(
            TlScenarioAlias(
                scenario_id=scenario.id, alias=f"{target['code'].upper()}_{counter:03d}_01"
            )
        )
        await s.commit()
    created = await _new_feature(client, target, actor)
    assert created["number"] == counter + 1

# ruff: noqa: S101, F811
"""Fase 4, Task 4: API NRT del Test Lifecycle."""
from __future__ import annotations

import uuid

import pytest

from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _new_scenario,
    _use_test_db,
    actor,
    scope,
)
from tests.tl_nrt_fixtures import nrt  # noqa: F401


async def test_feature_nrt_endpoint(client, scope, actor, nrt):
    feat = await _new_feature(client, scope, actor)
    sc = await _new_scenario(client, feat, actor)
    run_id = await nrt.run("UAT", [(sc["tag"].lstrip("@"), "PASSED")])
    resp = await client.get(f"{BASE}/features/{feat['id']}/nrt")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pass_rate"] == 1.0
    (only,) = body["scenarios"]
    assert only["tag"] == sc["tag"] and only["envs"][0]["run_id"] == str(run_id)

    links = await client.get(f"{BASE}/nrt/runs/{run_id}/links")
    assert links.status_code == 200
    assert [k["feature_code"] for k in links.json()] == [feat["code"]]

    scopes = (await client.get(f"{BASE}/scopes")).json()
    mine = next(s for s in scopes if s["id"] == scope["id"])
    assert mine["nrt_pass_rate"] == pytest.approx(1.0)


async def test_feature_nrt_404(client):
    resp = await client.get(f"{BASE}/features/{uuid.uuid4()}/nrt")
    assert resp.status_code == 404

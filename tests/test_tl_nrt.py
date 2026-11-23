# ruff: noqa: S101, F811
"""Fase 4, Task 2: esiti NRT per scenario (match per tag identità e alias)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.models.flaky import FlakyRegistry
from app.models.test_lifecycle import TlScenarioAlias
from app.services.test_lifecycle import nrt as nrt_svc
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    _new_feature,
    _new_scenario,
    _use_test_db,
    actor,
    scope,
)
from tests.tl_nrt_fixtures import nrt  # noqa: F401


async def _feature(client, scope_, actor, n=2):
    feat = await _new_feature(client, scope_, actor)
    scenarios = [await _new_scenario(client, feat, actor, title=f"S{i}") for i in range(1, n + 1)]
    return feat, scenarios


def _bare(tag: str) -> str:
    return tag.lstrip("@")


async def test_latest_result_per_env_and_pass_rate(client, scope, actor, nrt):
    feat, (s1, s2) = await _feature(client, scope, actor)
    t1 = _bare(s1["tag"])
    await nrt.run("UAT", [(t1, "FAILED")])
    run2 = await nrt.run("UAT", [(t1, "PASSED")])
    run3 = await nrt.run("DEV", [(t1, "PASSED"), ("OTHER_999_01", "FAILED")])
    async with TestSession() as db:
        view = await nrt_svc.feature_nrt(db, uuid.UUID(feat["id"]))
    first, second = view.scenarios
    assert first.tag == s1["tag"] and first.title == "S1"
    envs = {e.env: e for e in first.envs}
    assert envs["UAT"].status == "PASSED" and envs["UAT"].run_id == run2
    assert envs["DEV"].run_id == run3
    assert envs["UAT"].allure_url == "https://allure.test/case"
    assert first.runs == 3
    assert first.pass_rate == pytest.approx(2 / 3)
    assert second.envs == [] and second.pass_rate is None and second.runs == 0
    assert view.pass_rate == pytest.approx(2 / 3)
    assert view.last_run_at == first.last_run_at


async def test_outline_run_failed_if_any_example_fails(client, scope, actor, nrt):
    feat, (s1,) = await _feature(client, scope, actor, n=1)
    t1 = _bare(s1["tag"])
    await nrt.run("UAT", [(t1, "PASSED"), (t1, "FAILED"), (t1, "SKIPPED")])
    async with TestSession() as db:
        view = await nrt_svc.feature_nrt(db, uuid.UUID(feat["id"]))
    (only,) = view.scenarios
    assert only.runs == 1 and only.envs[0].status == "FAILED" and only.pass_rate == 0


async def test_skipped_only_run_does_not_count(client, scope, actor, nrt):
    feat, (s1,) = await _feature(client, scope, actor, n=1)
    await nrt.run("UAT", [(_bare(s1["tag"]), "SKIPPED")])
    async with TestSession() as db:
        view = await nrt_svc.feature_nrt(db, uuid.UUID(feat["id"]))
    (only,) = view.scenarios
    assert only.envs[0].status == "SKIPPED" and only.pass_rate is None


async def test_alias_matches_and_flaky_from_registry(client, scope, actor, nrt):
    feat, (s1,) = await _feature(client, scope, actor, n=1)
    alias = f"OLD_{uuid.uuid4().hex[:8]}"
    name = f"Scenario {uuid.uuid4().hex[:8]}"
    async with TestSession() as s:
        s.add(TlScenarioAlias(scenario_id=uuid.UUID(s1["id"]), alias=alias))
        s.add(FlakyRegistry(test_object="x", test_type="NRT", scenario_name=name, is_flaky=True))
        await s.commit()
    try:
        await nrt.run("UAT", [(alias, "PASSED")], name=name)
        async with TestSession() as db:
            view = await nrt_svc.feature_nrt(db, uuid.UUID(feat["id"]))
        (only,) = view.scenarios
        assert only.runs == 1 and only.flaky is True
    finally:
        async with TestSession() as s:
            await s.execute(delete(FlakyRegistry).where(FlakyRegistry.scenario_name == name))
            await s.commit()


async def test_run_links_and_scope_pass_rate(client, scope, actor, nrt):
    feat, (s1, _s2) = await _feature(client, scope, actor)
    run_id = await nrt.run("UAT", [(_bare(s1["tag"]), "PASSED"), ("OTHER_999_01", "FAILED")])
    async with TestSession() as db:
        links = await nrt_svc.run_feature_links(db, run_id)
        rates = await nrt_svc.scope_pass_rates(db)
    assert [(str(k.feature_id), k.feature_code, k.scenario_tag) for k in links] == [
        (feat["id"], feat["code"], s1["tag"])
    ]
    assert rates[uuid.UUID(scope["id"])] == pytest.approx(1.0)


async def test_feature_without_executions(client, scope, actor):
    feat, _ = await _feature(client, scope, actor, n=1)
    async with TestSession() as db:
        view = await nrt_svc.feature_nrt(db, uuid.UUID(feat["id"]))
    assert view.last_run_at is None and view.pass_rate is None

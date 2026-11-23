# ruff: noqa: S101, F811
"""Fase 4, Task 3: IMPLEMENTED → RUNNING alla prima esecuzione NRT."""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.test_lifecycle import TlFeature, TlFeatureStatus
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


async def _feature(client, scope_, actor, status):
    feat = await _new_feature(client, scope_, actor)
    sc = await _new_scenario(client, feat, actor)
    async with TestSession() as s:
        row = await s.get(TlFeature, uuid.UUID(feat["id"]))
        row.status = status
        await s.commit()
    return feat, sc["tag"].lstrip("@")


async def _state(fid):
    async with TestSession() as s:
        row = await s.get(TlFeature, uuid.UUID(fid))
        events = (
            await s.execute(
                select(func.count()).select_from(AuditLog).where(
                    AuditLog.resource_id == fid, AuditLog.action == "tl.feature.running"
                )
            )
        ).scalar_one()
        return row.status, row.running_at, events


async def test_implemented_becomes_running_once(client, scope, actor, nrt):
    feat, tag = await _feature(client, scope, actor, TlFeatureStatus.IMPLEMENTED)
    run_id = await nrt.run("UAT", [(tag, "FAILED")])
    async with TestSession() as db:
        assert await nrt_svc.promote_running(db, run_ids=[run_id]) == 1
    status, running_at, events = await _state(feat["id"])
    assert status == TlFeatureStatus.RUNNING and running_at is not None and events == 1
    async with TestSession() as db:
        assert await nrt_svc.promote_running(db, run_ids=[run_id]) == 0
    assert (await _state(feat["id"]))[2] == 1


async def test_designed_feature_stays_designed(client, scope, actor, nrt):
    feat, tag = await _feature(client, scope, actor, TlFeatureStatus.DESIGNED)
    run_id = await nrt.run("UAT", [(tag, "PASSED")])
    async with TestSession() as db:
        assert await nrt_svc.promote_running(db, run_ids=[run_id]) == 0
    assert (await _state(feat["id"]))[0] == TlFeatureStatus.DESIGNED


async def _ingest(client, run_id, tag):
    return await client.post(
        "/api/v1/test-metrics/test-executions",
        json=[{
            "run_id": str(run_id), "status": "PASSED", "scenario_name": "S",
            "allure_report": {"labels": [{"name": "tag", "value": tag}]},
        }],
    )


async def test_ingest_promotes_feature(client, scope, actor, nrt):
    feat, tag = await _feature(client, scope, actor, TlFeatureStatus.IMPLEMENTED)
    run_id = await nrt.run("DEV")
    resp = await _ingest(client, run_id, tag)
    assert resp.status_code == 200, resp.text
    assert (await _state(feat["id"]))[0] == TlFeatureStatus.RUNNING


async def test_ingest_survives_promotion_failure(client, scope, actor, nrt, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(nrt_svc, "promote_running", boom)
    feat, tag = await _feature(client, scope, actor, TlFeatureStatus.IMPLEMENTED)
    run_id = await nrt.run("DEV")
    resp = await _ingest(client, run_id, tag)
    assert resp.status_code == 200, resp.text
    assert (await _state(feat["id"]))[0] == TlFeatureStatus.IMPLEMENTED


def test_beat_entry():
    from app.tasks import schedule

    entry = schedule.celery_app.conf.beat_schedule["promote-test-lifecycle-running"]
    assert entry["task"] == "app.tasks.sync_test_lifecycle_nrt.promote_running"
    assert entry["schedule"] == settings.tl_nrt_running_interval_s
    assert entry["options"] == {"expires": settings.tl_nrt_running_interval_s}

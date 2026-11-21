# ruff: noqa: S101, F811
"""Aggiornamento periodico dello stato dei task Jira (fase 3, Task 6)."""
from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.models.test_lifecycle import TlFeature
from app.services.test_lifecycle import jira_tasks
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _use_test_db,
    actor,
    scope,
)
from tests.tl_fakes import FakeJiraClient, install_fake_jira


@pytest.fixture
def jira(monkeypatch):
    fake = install_fake_jira(monkeypatch, FakeJiraClient())
    fake.add_issue("PQ-12", "Progettazione", issue_type="Progettazione Scenari e casi di test")
    fake.add_issue("PQ-13", "Altro", issue_type="Progettazione Scenari e casi di test")
    return fake


async def _with_key(client, scope_, actor, key):
    feat = await _new_feature(client, scope_, actor)
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}",
        json={"actor_email": actor["email"], "jira_design_key": key},
    )
    assert resp.status_code == 200, resp.text
    return feat


async def _cache(fid):
    async with TestSession() as s:
        return (await s.get(TlFeature, uuid.UUID(fid))).jira_cache


async def test_refresh_all_dedups_keys(client, scope, actor, jira):
    a = await _with_key(client, scope, actor, "PQ-12")
    b = await _with_key(client, scope, actor, "PQ-12")
    jira.add_issue("PQ-12", "Progettazione", issue_type="Progettazione Scenari e casi di test",
                   status="Done", category="done")
    jira.calls.clear()
    async with TestSession() as db:
        updated = await jira_tasks.refresh_all(
            db, jira, feature_ids=[uuid.UUID(a["id"]), uuid.UUID(b["id"])]
        )
    assert updated == 2
    assert len(jira.calls_of("get_issue")) == 1
    assert (await _cache(a["id"]))["PQ-12"]["category"] == "done"
    assert (await _cache(b["id"]))["PQ-12"]["category"] == "done"


async def test_refresh_all_ignores_single_key_errors(client, scope, actor, jira):
    a = await _with_key(client, scope, actor, "PQ-12")
    b = await _with_key(client, scope, actor, "PQ-13")
    for key in ("PQ-12", "PQ-13"):
        jira.add_issue(key, "S", issue_type="Progettazione Scenari e casi di test",
                       status="Done", category="done")
    jira.calls.clear()
    jira.fail_once("get_issue", 502, "giù")  # la prima chiave letta fallisce, l'altra no
    async with TestSession() as db:
        updated = await jira_tasks.refresh_all(
            db, jira, feature_ids=[uuid.UUID(a["id"]), uuid.UUID(b["id"])]
        )
    assert updated == 1
    assert len(jira.calls_of("get_issue")) == 2
    done = [
        (await _cache(fid))[key]["category"] == "done"
        for fid, key in ((a["id"], "PQ-12"), (b["id"], "PQ-13"))
    ]
    assert sorted(done) == [False, True]  # la voce fallita resta com'era


async def test_task_not_configured_returns_status():
    from app.tasks.sync_test_lifecycle_jira import _async_refresh

    assert await _async_refresh() == {"status": "not_configured"}


def test_beat_entry():
    from app.tasks import schedule

    entry = schedule.celery_app.conf.beat_schedule["refresh-test-lifecycle-jira"]
    assert entry["task"] == "app.tasks.sync_test_lifecycle_jira.refresh_jira"
    assert entry["schedule"] == settings.tl_jira_refresh_interval_s
    assert entry["options"] == {"expires": settings.tl_jira_refresh_interval_s}

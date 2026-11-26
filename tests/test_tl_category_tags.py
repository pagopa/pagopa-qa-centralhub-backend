# ruff: noqa: S101, F811
"""Fase 5, Task 2: tag di categoria estendibili."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.models.test_lifecycle import TlCategoryTag
from app.services.test_lifecycle.category_tags import category_tags
from app.services.test_lifecycle.feature_parser import parse_feature_file
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _new_scenario,
    _use_test_db,
    actor,
    scope,
)

FILE = """# language: it
Funzionalità: Prova

  @X_001_01 @positivo @regression @runnable
  Scenario: Uno
    Dato qualcosa
"""


@pytest.fixture
async def regression_tag():
    async with TestSession() as s:
        s.add(TlCategoryTag(name="regression"))
        await s.commit()
    yield "regression"
    async with TestSession() as s:
        await s.execute(delete(TlCategoryTag).where(TlCategoryTag.name == "regression"))
        await s.commit()


async def test_category_tags_include_defaults_and_added(regression_tag):
    async with TestSession() as db:
        tags = await category_tags(db)
    assert {"runnable", "skip", "broken", "flaky", "regression"} <= set(tags)
    assert list(tags) == sorted(tags)


def test_parser_keeps_every_tag_unless_given_a_list():
    assert parse_feature_file(FILE).scenarios[0].extra_tags == ["regression", "runnable"]
    parsed = parse_feature_file(FILE, allowed_extra=("runnable",))
    assert parsed.scenarios[0].extra_tags == ["runnable"]


async def _patch(client, feat, sc, actor, tags, lock=1):
    rev = feat["working_revision"]["id"]
    return await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev}/scenarios/{sc['id']}",
        json={"actor_email": actor["email"], "lock_version": lock, "extra_tags": tags},
    )


async def test_scenario_accepts_catalog_and_custom_tags(client, scope, actor, regression_tag):
    feat = await _new_feature(client, scope, actor)
    sc = await _new_scenario(client, feat, actor)
    ok = await _patch(client, feat, sc, actor, ["regression"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["extra_tags"] == ["regression"]
    custom = f"t{uuid.uuid4().hex[:6]}"  # not in the catalog: the catalog is no longer a gate
    free = await _patch(client, feat, sc, actor, [custom], lock=2)
    assert free.status_code == 200, free.text
    assert free.json()["extra_tags"] == [custom]


async def test_category_tags_endpoint(client):
    resp = await client.get(f"{BASE}/category-tags")
    assert resp.status_code == 200
    assert {"runnable", "skip", "broken", "flaky"} <= set(resp.json())

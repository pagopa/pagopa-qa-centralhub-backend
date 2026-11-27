# ruff: noqa: F811, S101
"""The status shown for a feature follows the change in progress; NRT execution is a badge."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update

from app.models.test_lifecycle import TlFeature, TlFeatureStatus, TlRevisionStatus
from app.services.test_lifecycle.display_status import display_status
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _use_test_db,
    actor,
    scope,
)

S, R = TlFeatureStatus, TlRevisionStatus


@pytest.mark.parametrize(
    ("feature", "working", "shown"),
    [
        (S.RUNNING, None, S.IMPLEMENTED),
        (S.IMPLEMENTED, None, S.IMPLEMENTED),
        (S.RUNNING, R.DRAFT, S.DRAFT),
        (S.RUNNING, R.IN_REVIEW, S.IN_REVIEW),
        (S.RUNNING, R.APPROVED, S.DESIGNED),
        (S.IMPLEMENTED, R.DRAFT, S.DRAFT),
        (S.DRAFT, R.DRAFT, S.DRAFT),
        (S.DESIGNED, R.APPROVED, S.DESIGNED),
        (S.IN_REVIEW, R.IN_REVIEW, S.IN_REVIEW),
        (S.DESIGNED, None, S.DESIGNED),
    ],
)
def test_display_status(feature, working, shown):
    assert display_status(feature, working) == shown


async def _running_with_open_change(client, scope, actor):
    feat = await _new_feature(client, scope, actor)  # working revision in draft
    async with TestSession() as s:
        await s.execute(
            update(TlFeature).where(TlFeature.id == uuid.UUID(feat["id"]))
            .values(status=TlFeatureStatus.RUNNING)
        )
        await s.commit()
    return feat


async def test_list_and_detail_show_the_change_in_progress_and_the_running_badge(
    client, scope, actor
):
    feat = await _running_with_open_change(client, scope, actor)
    listed = next(
        f for f in (await client.get(f"{BASE}/features", params={"scope_id": scope["id"]})).json()
        if f["id"] == feat["id"]
    )
    assert listed["status"] == "running"  # the stored status is untouched
    assert listed["display_status"] == "draft" and listed["is_running"] is True
    detail = (await client.get(f"{BASE}/features/{feat['id']}")).json()
    assert detail["display_status"] == "draft" and detail["is_running"] is True


async def test_status_filter_uses_the_shown_status_and_running_is_the_badge(client, scope, actor):
    feat = await _running_with_open_change(client, scope, actor)

    async def ids(status):
        resp = await client.get(
            f"{BASE}/features", params={"scope_id": scope["id"], "status": status}
        )
        return [f["id"] for f in resp.json()]

    assert feat["id"] in await ids("draft")
    assert feat["id"] in await ids("running")
    assert feat["id"] not in await ids("implemented")


async def test_scope_counts_follow_the_shown_status_and_count_running_apart(client, scope, actor):
    await _running_with_open_change(client, scope, actor)
    row = next(
        r for r in (await client.get(f"{BASE}/scopes")).json() if r["id"] == scope["id"]
    )
    assert row["feature_count"] == 1
    assert row["status_counts"] == {"draft": 1, "in_review": 0, "designed": 0, "implemented": 0}
    assert row["running_count"] == 1

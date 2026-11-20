# ruff: noqa: F811, S101
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select, update

from app.models.audit_log import AuditLog
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureStatus,
    TlIssue,
    TlIssueKind,
    TlIssueStatus,
)
from app.services.test_lifecycle import issues as issues_svc
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _use_test_db,
    actor,
    scope,
)


async def _open(**kw):
    async with TestSession() as s:
        issue = await issues_svc.open_issue(s, **kw)
        await s.commit()
        return issue.id


async def test_open_issue_dedup(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    fid = uuid.UUID(feat["id"])
    a = await _open(
        kind=TlIssueKind.STRUCTURAL_CHANGE,
        feature_id=fid,
        path="f.feature",
        detail={"key": "k1", "n": 1},
    )
    b = await _open(
        kind=TlIssueKind.STRUCTURAL_CHANGE,
        feature_id=fid,
        path="f.feature",
        detail={"key": "k1", "n": 2},
    )
    c = await _open(
        kind=TlIssueKind.STRUCTURAL_CHANGE,
        feature_id=fid,
        path="f.feature",
        detail={"key": "k2"},
    )
    assert a == b
    assert c != a
    async with TestSession() as s:
        row = await s.get(TlIssue, a)
        assert row.detail == {"key": "k1", "n": 2}
        # una volta risolta, la stessa chiave apre una nuova anomalia
        row.status = TlIssueStatus.RESOLVED
        await s.commit()
    d = await _open(
        kind=TlIssueKind.STRUCTURAL_CHANGE,
        feature_id=fid,
        path="f.feature",
        detail={"key": "k1"},
    )
    assert d != a


async def test_list_filters(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    other = await _new_feature(client, scope, actor)
    fid, oid = uuid.UUID(feat["id"]), uuid.UUID(other["id"])
    i1 = await _open(kind=TlIssueKind.PARSE_ERROR, feature_id=fid, detail={})
    i2 = await _open(kind=TlIssueKind.FILE_DELETED, feature_id=oid, detail={})
    async with TestSession() as s:
        (await s.get(TlIssue, i2)).status = TlIssueStatus.ACKNOWLEDGED
        await s.commit()

    resp = await client.get(f"{BASE}/issues", params={"feature_id": str(fid)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [i["id"] for i in body] == [str(i1)]
    assert body[0]["feature_code"] == feat["code"]
    assert body[0]["feature_title"] == feat["working_revision"]["title"]

    resp = await client.get(
        f"{BASE}/issues", params={"feature_id": str(oid), "status": "acknowledged"}
    )
    assert [i["id"] for i in resp.json()] == [str(i2)]
    resp = await client.get(
        f"{BASE}/issues", params={"feature_id": str(oid), "kind": "parse_error"}
    )
    assert resp.json() == []
    resp = await client.get(f"{BASE}/issues", params={"feature_id": str(oid), "status": "open"})
    assert resp.json() == []

    both = await client.get(f"{BASE}/issues", params={"kind": "parse_error"})
    ids = [i["id"] for i in both.json()]
    assert str(i1) in ids
    created = [i["created_at"] for i in both.json()]
    assert created == sorted(created, reverse=True)


async def test_patch_acknowledge_records_event(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    fid = uuid.UUID(feat["id"])
    iid = await _open(kind=TlIssueKind.PARSE_ERROR, feature_id=fid, detail={})
    resp = await client.patch(
        f"{BASE}/issues/{iid}", json={"status": "acknowledged", "actor_email": actor["email"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "acknowledged"
    assert resp.json()["updated_by"] == actor["email"]
    async with TestSession() as s:
        ev = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.resource_id == str(fid), AuditLog.action == "tl.issue.updated"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(ev) == 1
    missing = await client.patch(
        f"{BASE}/issues/{uuid.uuid4()}",
        json={"status": "resolved", "actor_email": actor["email"]},
    )
    assert missing.status_code == 404


async def test_resolve_last_structural_sets_implemented(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    fid = uuid.UUID(feat["id"])
    async with TestSession() as s:
        rev_id = uuid.UUID(feat["working_revision"]["id"])
        await s.execute(
            update(TlFeature)
            .where(TlFeature.id == fid)
            .values(status=TlFeatureStatus.DESIGNED, published_revision_id=rev_id)
        )
        await s.commit()
    i1 = await _open(kind=TlIssueKind.STRUCTURAL_CHANGE, feature_id=fid, detail={"key": "a"})
    i2 = await _open(kind=TlIssueKind.STRUCTURAL_CHANGE, feature_id=fid, detail={"key": "b"})
    body = {"status": "resolved", "actor_email": actor["email"]}
    assert (await client.patch(f"{BASE}/issues/{i1}", json=body)).status_code == 200
    async with TestSession() as s:
        assert (await s.get(TlFeature, fid)).status == TlFeatureStatus.DESIGNED
    assert (await client.patch(f"{BASE}/issues/{i2}", json=body)).status_code == 200
    async with TestSession() as s:
        assert (await s.get(TlFeature, fid)).status == TlFeatureStatus.IMPLEMENTED
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.resource_id == str(fid))))
            .scalars()
            .all()
        )
        assert actions.count("tl.issue.updated") == 2
        assert "tl.feature.implemented" in actions


async def test_resolve_structural_without_published_revision_keeps_status(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    fid = uuid.UUID(feat["id"])
    async with TestSession() as s:
        await s.execute(
            update(TlFeature).where(TlFeature.id == fid).values(status=TlFeatureStatus.DESIGNED)
        )
        await s.commit()
    i1 = await _open(kind=TlIssueKind.STRUCTURAL_CHANGE, feature_id=fid, detail={})
    resp = await client.patch(
        f"{BASE}/issues/{i1}", json={"status": "resolved", "actor_email": actor["email"]}
    )
    assert resp.status_code == 200
    async with TestSession() as s:
        assert (await s.get(TlFeature, fid)).status == TlFeatureStatus.DESIGNED


async def test_unknown_actor_403(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    iid = await _open(kind=TlIssueKind.PARSE_ERROR, feature_id=uuid.UUID(feat["id"]), detail={})
    resp = await client.patch(
        f"{BASE}/issues/{iid}", json={"status": "resolved", "actor_email": "nobody@example.test"}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "unknown_actor"
    async with TestSession() as s:
        assert (await s.get(TlIssue, iid)).status == TlIssueStatus.OPEN


async def test_patch_rejects_invalid_status(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    iid = await _open(kind=TlIssueKind.PARSE_ERROR, feature_id=uuid.UUID(feat["id"]), detail={})
    resp = await client.patch(
        f"{BASE}/issues/{iid}", json={"status": "open", "actor_email": actor["email"]}
    )
    assert resp.status_code == 422


async def test_patch_idempotent_and_resolved_is_final(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    fid = uuid.UUID(feat["id"])
    iid = await _open(kind=TlIssueKind.PARSE_ERROR, feature_id=fid, detail={})
    body = {"status": "resolved", "actor_email": actor["email"]}
    assert (await client.patch(f"{BASE}/issues/{iid}", json=body)).status_code == 200
    again = await client.patch(f"{BASE}/issues/{iid}", json=body)
    assert again.status_code == 200 and again.json()["status"] == "resolved"
    back = await client.patch(
        f"{BASE}/issues/{iid}", json={"status": "acknowledged", "actor_email": actor["email"]}
    )
    assert back.status_code == 409
    assert back.json()["detail"]["code"] == "issue_already_resolved"
    async with TestSession() as s:
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.resource_id == str(fid))))
            .scalars()
            .all()
        )
        assert actions.count("tl.issue.updated") == 1


async def test_parallel_resolve_last_two_structural(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    fid = uuid.UUID(feat["id"])
    async with TestSession() as s:
        await s.execute(
            update(TlFeature)
            .where(TlFeature.id == fid)
            .values(
                status=TlFeatureStatus.DESIGNED,
                published_revision_id=uuid.UUID(feat["working_revision"]["id"]),
            )
        )
        await s.commit()
    ids = [
        await _open(kind=TlIssueKind.STRUCTURAL_CHANGE, feature_id=fid, detail={"key": k})
        for k in "ab"
    ]
    body = {"status": "resolved", "actor_email": actor["email"]}
    rs = await asyncio.gather(*(client.patch(f"{BASE}/issues/{i}", json=body) for i in ids))
    assert [r.status_code for r in rs] == [200, 200]
    async with TestSession() as s:
        assert (await s.get(TlFeature, fid)).status == TlFeatureStatus.IMPLEMENTED
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.resource_id == str(fid))))
            .scalars()
            .all()
        )
        assert actions.count("tl.feature.implemented") == 1
        assert actions.count("tl.issue.updated") == 2

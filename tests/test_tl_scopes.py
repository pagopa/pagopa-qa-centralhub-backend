from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.deps import get_db
from app.main import app
from app.models.test_lifecycle import TlFeature, TlFeatureStatus, TlScope
from tests._db import TestSession, get_test_db


def _code() -> str:
    return "T" + uuid.uuid4().hex[:10]


@pytest.fixture(autouse=True)
def _use_test_db():
    app.dependency_overrides[get_db] = get_test_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
async def cleanup():
    ids: list[str] = []
    yield ids
    async with TestSession() as s:
        for sid in ids:
            await s.execute(delete(TlScope).where(TlScope.id == uuid.UUID(sid)))
        await s.commit()


async def _create(client, cleanup, code=None, **extra):
    resp = await client.post(
        "/api/v1/test-lifecycle/scopes",
        json={"code": code or _code(), "name": "Ambito test", **extra},
    )
    if resp.status_code == 201:
        cleanup.append(resp.json()["id"])
    return resp


async def _add_feature(scope_id: str, status: TlFeatureStatus = TlFeatureStatus.DRAFT, n: int = 1):
    async with TestSession() as s:
        s.add(
            TlFeature(
                scope_id=uuid.UUID(scope_id),
                number=n,
                code=f"FEAT_{n:03d}_{uuid.uuid4().hex[:8]}",
                status=status,
                created_by="test@example.test",
            )
        )
        await s.commit()


async def test_create_scope_returns_201(client, cleanup):
    code = _code()
    resp = await _create(client, cleanup, code, area="Pagamenti")
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == code
    assert body["area"] == "Pagamenti"
    assert body["feature_count"] == 0
    assert body["status_counts"] == {
        "draft": 0, "in_review": 0, "designed": 0, "implemented": 0, "running": 0
    }


@pytest.mark.parametrize("bad", ["1abc", "FdR-3", "a", "A" * 21])
async def test_create_scope_rejects_invalid_code(client, cleanup, bad):
    resp = await _create(client, cleanup, bad)
    assert resp.status_code == 422


async def test_create_scope_duplicate_code_case_insensitive_returns_409(client, cleanup):
    code = _code()
    assert (await _create(client, cleanup, code)).status_code == 201
    resp = await _create(client, cleanup, code.lower())
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "scope_code_conflict"


async def test_scope_code_is_immutable_once_features_exist(client, cleanup):
    scope = (await _create(client, cleanup)).json()
    await _add_feature(scope["id"])
    resp = await client.patch(
        f"/api/v1/test-lifecycle/scopes/{scope['id']}", json={"code": _code()}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "code": "scope_code_locked",
        "message": "Il codice dell'ambito non può cambiare: esistono già feature",
    }


async def test_scope_code_editable_without_features_and_conflict(client, cleanup):
    a = (await _create(client, cleanup)).json()
    b = (await _create(client, cleanup)).json()
    new = _code()
    ok = await client.patch(f"/api/v1/test-lifecycle/scopes/{a['id']}", json={"code": new})
    assert ok.status_code == 200 and ok.json()["code"] == new
    dup = await client.patch(
        f"/api/v1/test-lifecycle/scopes/{b['id']}", json={"code": new.upper()}
    )
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "scope_code_conflict"


async def test_scope_name_editable_with_features(client, cleanup):
    scope = (await _create(client, cleanup)).json()
    await _add_feature(scope["id"])
    resp = await client.patch(
        f"/api/v1/test-lifecycle/scopes/{scope['id']}",
        json={"name": "Nuovo nome", "code": scope["code"]},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Nuovo nome"


async def test_patch_unknown_scope_returns_404(client):
    resp = await client.patch(
        f"/api/v1/test-lifecycle/scopes/{uuid.uuid4()}", json={"name": "x"}
    )
    assert resp.status_code == 404


async def test_list_scopes_includes_status_counts(client, cleanup):
    scope = (await _create(client, cleanup)).json()
    await _add_feature(scope["id"], TlFeatureStatus.DRAFT, 1)
    await _add_feature(scope["id"], TlFeatureStatus.DRAFT, 2)
    await _add_feature(scope["id"], TlFeatureStatus.DESIGNED, 3)
    resp = await client.get("/api/v1/test-lifecycle/scopes")
    assert resp.status_code == 200
    row = next(r for r in resp.json() if r["id"] == scope["id"])
    assert row["feature_count"] == 3
    assert row["status_counts"] == {
        "draft": 2, "in_review": 0, "designed": 1, "implemented": 0, "running": 0
    }

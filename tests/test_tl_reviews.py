# ruff: noqa: F811, S101
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models.audit_log import AuditLog
from app.models.test_lifecycle import TlReviewComment
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _new_scenario,
    _use_test_db,
    actor,
    scope,
)
from tests.test_tl_workflow import (  # noqa: F401
    VALID_STEPS,
    _do,
    _ready_feature,
    reviewer,
)


def _url(feat, rev_id=None, suffix="comments"):
    rev = rev_id or feat["working_revision"]["id"]
    return f"{BASE}/features/{feat['id']}/revisions/{rev}/{suffix}"


async def _events(feature_id: str, action: str) -> int:
    async with TestSession() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.resource_type == "tl_feature",
                    AuditLog.resource_id == feature_id,
                    AuditLog.action == action,
                )
            )
        ).scalar_one()


async def test_add_comment_on_scenario(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    scen = await _new_scenario(client, feat, actor)
    resp = await client.post(
        _url(feat),
        json={
            "actor_email": actor["email"],
            "body": "  Manca il caso KO  ",
            "scenario_id": scen["id"],
        },
    )
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["body"] == "Manca il caso KO"
    assert out["scenario_id"] == scen["id"]
    assert out["scenario_tag"] == scen["tag"]
    assert out["author"] == actor["email"]
    assert out["resolved"] is False

    general = await client.post(
        _url(feat), json={"actor_email": actor["email"], "body": "Generale"}
    )
    assert general.status_code == 201
    assert general.json()["scenario_id"] is None

    listed = await client.get(_url(feat))
    assert listed.status_code == 200
    assert [c["body"] for c in listed.json()] == ["Manca il caso KO", "Generale"]
    assert await _events(feat["id"], "tl.comment.added") == 2


async def test_comment_validation_and_guards(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    other = await _new_feature(client, scope, actor, title="Altra")
    other_scen = await _new_scenario(client, other, actor)
    for body in ("   ", "", "x" * 5001):
        resp = await client.post(_url(feat), json={"actor_email": actor["email"], "body": body})
        assert resp.status_code == 422, body
    resp = await client.post(_url(feat), json={"actor_email": "ghost@example.test", "body": "ciao"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "unknown_actor"
    resp = await client.post(
        _url(feat),
        json={"actor_email": actor["email"], "body": "ciao", "scenario_id": other_scen["id"]},
    )
    assert resp.status_code == 404
    resp = await client.post(
        _url(feat, str(uuid.uuid4())), json={"actor_email": actor["email"], "body": "ciao"}
    )
    assert resp.status_code == 404
    resp = await client.get(_url(feat, str(uuid.uuid4())))
    assert resp.status_code == 404


async def test_comment_on_approved_revision_returns_409(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    rev_id = detail["working_revision"]["id"]
    assert (await _do(client, detail, "submit", actor["email"])).status_code == 200
    # in_review: consentito
    resp = await client.post(
        _url(detail, rev_id), json={"actor_email": reviewer["email"], "body": "Ok in review"}
    )
    assert resp.status_code == 201
    assert (await _do(client, detail, "approve", reviewer["email"])).status_code == 200
    resp = await client.post(
        _url(detail, rev_id), json={"actor_email": reviewer["email"], "body": "Troppo tardi"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "revision_not_commentable"
    assert resp.json()["detail"]["message"]
    # la lettura resta possibile
    assert (await client.get(_url(detail, rev_id))).status_code == 200


async def test_resolve_comment(client, scope, actor, reviewer):
    feat = await _new_feature(client, scope, actor)
    created = (
        await client.post(_url(feat), json={"actor_email": actor["email"], "body": "Da sistemare"})
    ).json()
    url = f"{BASE}/comments/{created['id']}"
    resp = await client.patch(url, json={"actor_email": reviewer["email"], "resolved": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolved"] is True
    assert await _events(feat["id"], "tl.comment.resolved") == 1
    resp = await client.patch(url, json={"actor_email": reviewer["email"], "resolved": False})
    assert resp.json()["resolved"] is False
    assert await _events(feat["id"], "tl.comment.reopened") == 1
    resp = await client.patch(url, json={"actor_email": "ghost@example.test", "resolved": True})
    assert resp.status_code == 403
    resp = await client.patch(
        f"{BASE}/comments/{uuid.uuid4()}", json={"actor_email": actor["email"], "resolved": True}
    )
    assert resp.status_code == 404
    async with TestSession() as s:
        row = (
            await s.execute(
                select(TlReviewComment).where(TlReviewComment.id == uuid.UUID(created["id"]))
            )
        ).scalar_one()
    assert row.resolved is False


async def test_preview_returns_rendered_file_and_errors(client, scope, actor):
    feat = await _new_feature(client, scope, actor, design_owner_id=actor["id"])
    rev = feat["working_revision"]
    scen = await _new_scenario(client, feat, actor)
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev['id']}/scenarios/{scen['id']}",
        json={"lock_version": 1, "steps": "Dato un utente\nfoo bar", "actor_email": actor["email"]},
    )
    assert resp.status_code == 200, resp.text
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev['id']}",
        json={
            "lock_version": 2,
            "background_steps": "Quando sbagliato",
            "actor_email": actor["email"],
        },
    )
    assert resp.status_code == 200, resp.text
    resp = await client.get(_url(feat, suffix="preview"))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["file"].startswith(f"# qa-hub: {feat['code']}\n# language: it\n")
    assert scen["tag"] in out["file"]
    tags = {e["scenario_tag"] for e in out["errors"]}
    assert None in tags  # Contesto
    assert scen["tag"] in tags
    for e in out["errors"]:
        assert isinstance(e["line"], int) and e["message"]


async def test_preview_works_for_approved_revision(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    await _do(client, detail, "submit", actor["email"])
    await _do(client, detail, "approve", reviewer["email"])
    resp = await client.get(_url(detail, suffix="preview"))
    assert resp.status_code == 200
    assert resp.json()["errors"] == []
    assert "Allora il pagamento è confermato" in resp.json()["file"]


async def test_validate_endpoint_returns_errors_without_db_writes(client, scope, actor):
    async def counts():
        async with TestSession() as s:
            return (
                (await s.execute(select(func.count()).select_from(AuditLog))).scalar_one(),
                (await s.execute(select(func.count()).select_from(TlReviewComment))).scalar_one(),
            )

    before = await counts()
    resp = await client.post(
        f"{BASE}/gherkin/validate", json={"steps": "Quando paga\nfoo bar", "kind": "scenario"}
    )
    assert resp.status_code == 200, resp.text
    errors = resp.json()["errors"]
    assert errors and all({"line", "message"} <= set(e) for e in errors)
    ok = await client.post(
        f"{BASE}/gherkin/validate", json={"steps": VALID_STEPS, "kind": "scenario"}
    )
    assert ok.json() == {"errors": []}
    bg = await client.post(
        f"{BASE}/gherkin/validate",
        json={"steps": "Quando x", "kind": "scenario", "background": True},
    )
    assert bg.json()["errors"]
    bad = await client.post(
        f"{BASE}/gherkin/validate",
        json={
            "steps": VALID_STEPS,
            "kind": "outline",
            "examples": {"headers": ["a"], "rows": [["1", "2"]]},
        },
    )
    assert bad.status_code == 422
    assert await counts() == before


def test_validate_route_has_no_db_dependency():
    from app.main import app

    route = next(r for r in app.routes if getattr(r, "path", "").endswith("/gherkin/validate"))
    assert route.dependant.dependencies == []

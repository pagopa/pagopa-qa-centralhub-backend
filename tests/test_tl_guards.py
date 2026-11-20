# ruff: noqa: F811, S101
"""Guardie della review finale di fase 1 (R17 e minori): assegnazioni, link, note, commenti."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from gherkin.parser import Parser
from sqlalchemy import delete, select, update

from app.models.audit_log import AuditLog
from app.models.test_lifecycle import TlFeature, TlPolarity, TlScenarioKind, TlScope
from app.models.user import User
from app.schemas.test_lifecycle import TlScopeUpdate
from app.services.test_lifecycle import scopes as scopes_svc
from app.services.test_lifecycle.gherkin import RenderFeature, RenderScenario, render_feature_file
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _new_scenario,
    _use_test_db,
    actor,
    scope,
)
from tests.test_tl_reviews import _events, _url
from tests.test_tl_workflow import (  # noqa: F401
    _do,
    _get,
    _ready_feature,
    _transition,
    reviewer,
)

INJECTION = "https://x\n  Scenario: iniettato\n    Dato y"


async def _make_user(active: bool = True) -> dict:
    email = f"tl-g-{uuid.uuid4().hex[:10]}@example.test"
    async with TestSession() as s:
        user = User(email=email, name="TL Guard", role="qa_analyst", is_active=active)
        s.add(user)
        await s.commit()
        return {"id": str(user.id), "email": email}


async def _drop_user(user: dict) -> None:
    user_id = uuid.UUID(user["id"])
    async with TestSession() as s:
        for col in (TlFeature.reviewer_id, TlFeature.design_owner_id, TlFeature.impl_owner_id):
            await s.execute(update(TlFeature).where(col == user_id).values({col.key: None}))
        await s.execute(delete(AuditLog).where(AuditLog.actor_id == user_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


@pytest.fixture
async def other():
    user = await _make_user()
    yield user
    await _drop_user(user)


@pytest.fixture
async def inactive():
    user = await _make_user(active=False)
    yield user
    await _drop_user(user)


def _patch(client, feat_id, actor, **fields):
    return client.patch(
        f"{BASE}/features/{feat_id}", json={"actor_email": actor["email"], **fields}
    )


# ── Finding 2: campi della feature bloccati fuori dalla bozza ────────────────

async def test_owner_and_design_key_locked_outside_draft(client, scope, actor, reviewer, other):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    for fields in ({"design_owner_id": other["id"]}, {"jira_design_key": "PQ-99"}):
        resp = await _patch(client, out["id"], actor, **fields)
        assert resp.status_code == 409, fields
        assert resp.json()["detail"]["code"] == "revision_locked"
    # PATCH idempotente (valori invariati) non è bloccato
    resp = await _patch(client, out["id"], actor, jira_design_key="PQ-12")
    assert resp.status_code == 200, resp.text
    # owner implementazione e chiave Jira implementazione restano liberi
    resp = await _patch(
        client, out["id"], actor, impl_owner_id=other["id"], jira_impl_key="ABC-7"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["impl_owner_id"] == other["id"]
    assert resp.json()["design_owner_id"] == actor["id"]
    assert resp.json()["jira_design_key"] == "PQ-12"


async def test_reviewer_reassignable_during_review(client, scope, actor, reviewer, other):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    before = await _events(out["id"], "tl.feature.updated")
    resp = await _patch(client, out["id"], actor, reviewer_id=other["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["reviewer_id"] == other["id"]
    assert await _events(out["id"], "tl.feature.updated") == before + 1
    # reviewer == owner, reviewer rimosso: 422 con codice chiaro
    resp = await _patch(client, out["id"], actor, reviewer_id=actor["id"])
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "reviewer_is_owner"
    resp = await _patch(client, out["id"], actor, reviewer_id=None)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "reviewer_required"
    # il vecchio reviewer non può più approvare, il nuovo sì
    resp = await _do(client, out, "approve", reviewer["email"])
    assert resp.status_code == 403
    resp = await _do(client, out, "approve", other["email"])
    assert resp.status_code == 200, resp.text
    # dopo l'approvazione il reviewer non si cambia più
    resp = await _patch(client, out["id"], actor, reviewer_id=reviewer["id"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "revision_locked"


async def test_owner_cannot_become_reviewer_in_draft(client, scope, actor, reviewer):
    feat = await _new_feature(client, scope, actor, design_owner_id=actor["id"])
    assert (await _patch(client, feat["id"], actor, reviewer_id=reviewer["id"])).status_code == 200
    resp = await _patch(client, feat["id"], actor, design_owner_id=reviewer["id"])
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "reviewer_is_owner"


async def test_approve_blocked_when_reviewer_equals_owner(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    # Stato incoerente forzato a DB (es. dati legacy): l'approvazione resta bloccata.
    async with TestSession() as s:
        await s.execute(
            update(TlFeature)
            .where(TlFeature.id == uuid.UUID(out["id"]))
            .values(reviewer_id=uuid.UUID(actor["id"]))
        )
        await s.commit()
    view = await _get(client, out["id"], actor["email"])
    approve = _transition(view, "approve")
    assert approve["allowed"] is False
    assert approve["reasons"] == ["Il reviewer coincide con l'owner di progettazione"]
    resp = await _do(client, out, "approve", actor["email"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "transition_not_allowed"
    assert (await _get(client, out["id"]))["working_revision"]["status"] == "in_review"


# ── Finding 4: utenti non attivi rifiutati ──────────────────────────────────

async def test_inactive_users_rejected(client, scope, actor, inactive):
    resp = await client.post(
        f"{BASE}/features",
        json={
            "scope_id": scope["id"],
            "title": "X",
            "actor_email": actor["email"],
            "design_owner_id": inactive["id"],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unknown_user"
    feat = await _new_feature(client, scope, actor, design_owner_id=actor["id"])
    for field in ("reviewer_id", "impl_owner_id", "design_owner_id"):
        resp = await _patch(client, feat["id"], actor, **{field: inactive["id"]})
        assert resp.status_code == 422, field
        assert resp.json()["detail"]["code"] == "unknown_user"


async def test_inactive_reviewer_rejected_during_review(client, scope, actor, reviewer, inactive):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    resp = await _patch(client, out["id"], actor, reviewer_id=inactive["id"])
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unknown_user"


# ── Finding 3: link su una riga sola, solo http(s) ──────────────────────────

@pytest.mark.parametrize(
    "value",
    [INJECTION, "https://x\r\nfoo", "ftp://example.test/a", "javascript:alert(1)", "x", "https://"],
)
async def test_links_must_be_single_line_http(client, scope, actor, value):
    feat = await _new_feature(client, scope, actor)
    rev = feat["working_revision"]["id"]
    sc = await _new_scenario(client, feat, actor)
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev}/scenarios/{sc['id']}",
        json={"actor_email": actor["email"], "lock_version": 1, "detail_link": value},
    )
    assert resp.status_code == 422, value
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev}",
        json={"actor_email": actor["email"], "lock_version": 1, "requirements_url": value},
    )
    assert resp.status_code == 422, value


async def test_links_trimmed_and_empty_becomes_null(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    rev = feat["working_revision"]["id"]
    sc = await _new_scenario(client, feat, actor)
    url = f"{BASE}/features/{feat['id']}/revisions/{rev}/scenarios/{sc['id']}"
    resp = await client.patch(
        url,
        json={
            "actor_email": actor["email"],
            "lock_version": 1,
            "detail_link": "  HTTPS://req.example.test/42  ",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["detail_link"] == "HTTPS://req.example.test/42"
    resp = await client.patch(
        url, json={"actor_email": actor["email"], "lock_version": 2, "detail_link": "   "}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["detail_link"] is None
    rev_url = f"{BASE}/features/{feat['id']}/revisions/{rev}"
    resp = await client.patch(
        rev_url,
        json={
            "actor_email": actor["email"],
            "lock_version": 3,
            "requirements_url": " http://req.example.test ",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["requirements_url"] == "http://req.example.test"
    resp = await client.patch(
        rev_url, json={"actor_email": actor["email"], "lock_version": 4, "requirements_url": ""}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["requirements_url"] is None


def test_render_never_emits_newline_from_detail_link():
    feature = RenderFeature(
        code="FEAT_001_X",
        feature_tag="@X_001",
        title="Pagamento",
        description=None,
        background_steps=None,
        scenarios=[
            RenderScenario(
                tag="@X_001_01",
                title="Paga",
                kind=TlScenarioKind.SCENARIO,
                polarity=TlPolarity.POSITIVO,
                extra_tags=[],
                steps="Dato un carrello\nQuando paga\nAllora vede la conferma",
                examples=None,
                detail_link=INJECTION,
            )
        ],
    )
    text = render_feature_file(feature)
    assert "iniettato" not in text
    assert "  #link:https://x\n" in text
    doc = Parser().parse(text)
    assert len(doc["feature"]["children"]) == 1


# ── Minori: nota di richiesta modifiche, risoluzione commenti, lock ambito ──

async def test_request_changes_note_max_length(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    resp = await _do(client, out, "request_changes", reviewer["email"], note="x" * 5001)
    assert resp.status_code == 422
    resp = await _do(client, out, "request_changes", reviewer["email"], note="x" * 5000)
    assert resp.status_code == 200, resp.text


async def test_resolve_refused_on_non_commentable_revision(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    rev_id = detail["working_revision"]["id"]
    created = (
        await client.post(
            _url(detail, rev_id), json={"actor_email": actor["email"], "body": "Da vedere"}
        )
    ).json()
    out = (await _do(client, detail, "submit", actor["email"])).json()
    url = f"{BASE}/comments/{created['id']}"
    # in review: consentito
    resp = await client.patch(url, json={"actor_email": reviewer["email"], "resolved": True})
    assert resp.status_code == 200, resp.text
    assert (await _do(client, out, "approve", reviewer["email"])).status_code == 200
    resp = await client.patch(url, json={"actor_email": reviewer["email"], "resolved": False})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "revision_not_commentable"
    comments = (await client.get(_url(detail, rev_id))).json()
    assert comments[0]["resolved"] is True


async def test_update_scope_waits_for_concurrent_feature_creation(client, scope, actor):
    """update_scope blocca la riga dell'ambito: vede la feature creata in concorrenza."""
    scope_id = uuid.UUID(scope["id"])
    async with TestSession() as holder:
        # Simula create_feature in corso: lock sull'ambito + feature non ancora committata.
        locked = (
            await holder.execute(select(TlScope).where(TlScope.id == scope_id).with_for_update())
        ).scalar_one()
        holder.add(
            TlFeature(
                scope_id=scope_id,
                number=locked.next_feature_number,
                code=f"FEAT_{locked.next_feature_number:03d}_{locked.code}",
                created_by=actor["email"],
            )
        )
        locked.next_feature_number += 1
        await holder.flush()

        async def rename():
            async with TestSession() as s:
                return await scopes_svc.update_scope(
                    s, scope_id, TlScopeUpdate(code="R" + uuid.uuid4().hex[:8])
                )

        task = asyncio.create_task(rename())
        await asyncio.sleep(0.3)
        assert not task.done(), "update_scope deve attendere il lock dell'ambito"
        await holder.commit()
    with pytest.raises(scopes_svc.ScopeCodeLockedError):
        await asyncio.wait_for(task, timeout=5)

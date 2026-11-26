# ruff: noqa: F811, S101
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, update

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureStatus,
    TlFeatureVersion,
    TlPolarity,
    TlReviewComment,
    TlRevisionStatus,
    TlScenarioKind,
)
from app.models.user import User
from app.services.test_lifecycle.advisory_lock import try_advisory_lock
from app.services.test_lifecycle.gherkin import (
    RenderFeature,
    RenderScenario,
    feature_tag,
    render_feature_file,
)
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _new_feature,
    _new_scenario,
    _use_test_db,
    actor,
    scope,
)
from tests.tl_fakes import FakeGitClient, install_fake_git

REASON_REOPEN = "Disponibile dopo il merge del feature file (fase Git)"
VALID_STEPS = "Dato un utente registrato\nQuando paga\nAllora il pagamento è confermato"


@pytest.fixture
async def reviewer():
    email = f"tl-rev-{uuid.uuid4().hex[:10]}@example.test"
    async with TestSession() as s:
        user = User(email=email, name="TL Reviewer", role="qa_analyst", is_active=True)
        s.add(user)
        await s.commit()
        user_id = user.id
    yield {"id": str(user_id), "email": email}
    async with TestSession() as s:
        await s.execute(
            update(TlFeature).where(TlFeature.reviewer_id == user_id).values(reviewer_id=None)
        )
        await s.execute(delete(AuditLog).where(AuditLog.actor_id == user_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


def _transition(detail, action):
    return next(t for t in detail["transitions"] if t["action"] == action)


async def _get(client, feat_id, actor_email=None):
    params = {"actor_email": actor_email} if actor_email else None
    resp = await client.get(f"{BASE}/features/{feat_id}", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _ready_feature(client, scope, actor, reviewer):
    """Feature con tutto il necessario per il submit."""
    feat = await _new_feature(client, scope, actor, design_owner_id=actor["id"])
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}",
        json={
            "reviewer_id": reviewer["id"],
            "jira_design_key": "PQ-12",
            "actor_email": actor["email"],
        },
    )
    assert resp.status_code == 200, resp.text
    rev = feat["working_revision"]
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev['id']}",
        json={"lock_version": 0, "product_issue_key": "ABC-1", "actor_email": actor["email"]},
    )
    assert resp.status_code == 200, resp.text
    scen = await _new_scenario(client, feat, actor)
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}/revisions/{rev['id']}/scenarios/{scen['id']}",
        json={"lock_version": 2, "steps": VALID_STEPS, "actor_email": actor["email"]},
    )
    assert resp.status_code == 200, resp.text
    return await _get(client, feat["id"], actor["email"])


async def _do(client, detail, action, email, note=None):
    rev = detail["working_revision"]["id"]
    body = {"action": action, "actor_email": email}
    if note is not None:
        body["note"] = note
    url = f"{BASE}/features/{detail['id']}/revisions/{rev}/transitions"
    return await client.post(url, json=body)


async def test_submit_blocked_reasons_listed(client, scope, actor):
    feat = await _new_feature(client, scope, actor)
    detail = await _get(client, feat["id"], actor["email"])
    assert [t["action"] for t in detail["transitions"]] == [
        "submit", "approve", "request_changes", "reopen", "withdraw_approval", "withdraw",
    ]
    submit = _transition(detail, "submit")
    assert submit["allowed"] is False
    assert submit["reasons"] == [
        "Manca l'owner di progettazione",
        "Manca il reviewer",
        "Manca la chiave Jira di progettazione",
        "Manca l'Epic/Story prodotto (o il flag reverse engineering)",
        "Serve almeno uno scenario",
    ]
    resp = await _do(client, detail, "submit", actor["email"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "transition_not_allowed"
    assert resp.json()["detail"]["reasons"] == submit["reasons"]


async def test_submit_blocked_when_reviewer_is_owner(client, scope, actor):
    feat = await _new_feature(client, scope, actor, design_owner_id=actor["id"])
    resp = await client.patch(
        f"{BASE}/features/{feat['id']}",
        json={"reviewer_id": actor["id"], "actor_email": actor["email"]},
    )
    # R17: l'API rifiuta reviewer == owner; la guardia del submit resta come difesa.
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "reviewer_is_owner"
    async with TestSession() as s:
        await s.execute(
            update(TlFeature)
            .where(TlFeature.id == uuid.UUID(feat["id"]))
            .values(reviewer_id=uuid.UUID(actor["id"]))
        )
        await s.commit()
    detail = await _get(client, feat["id"], actor["email"])
    reasons = _transition(detail, "submit")["reasons"]
    assert "Il reviewer coincide con l'owner di progettazione" in reasons
    assert "Manca il reviewer" not in reasons


async def test_submit_blocked_by_invalid_steps(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    rev = detail["working_revision"]
    scen2 = await _new_scenario(client, detail, actor, title="Vuoto")  # step vuoti: non validi
    detail = await _get(client, detail["id"], actor["email"])
    submit = _transition(detail, "submit")
    assert submit["allowed"] is False
    assert submit["reasons"] == [f"Lo scenario {scen2['tag']} ha step non validi"]
    # Contesto non valido
    lock = detail["working_revision"]["lock_version"]
    await client.patch(
        f"{BASE}/features/{detail['id']}/revisions/{rev['id']}",
        json={
            "lock_version": lock,
            "background_steps": "Quando sbagliato",
            "actor_email": actor["email"],
        },
    )
    detail = await _get(client, detail["id"], actor["email"])
    assert "Il Contesto contiene errori" in _transition(detail, "submit")["reasons"]


async def test_submit_moves_feature_to_in_review(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    assert _transition(detail, "submit")["allowed"] is True
    resp = await _do(client, detail, "submit", actor["email"])
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "in_review"
    assert out["working_revision"]["status"] == "in_review"
    assert _transition(out, "submit")["allowed"] is False
    async with TestSession() as s:
        logs = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.resource_id == detail["id"], AuditLog.action == "tl.revision.submitted"
                )
            )
        ).scalars().all()
    assert len(logs) == 1


async def test_edit_revision_in_review_returns_409(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    rev = out["working_revision"]
    resp = await client.patch(
        f"{BASE}/features/{out['id']}/revisions/{rev['id']}",
        json={"lock_version": rev["lock_version"], "title": "Nuovo", "actor_email": actor["email"]},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "revision_locked"


async def test_approve_by_non_reviewer_is_forbidden(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    # transitions senza actor / con actor non reviewer
    anon = await _get(client, out["id"])
    assert _transition(anon, "approve")["allowed"] is False
    assert _transition(anon, "approve")["reasons"] == ["Accesso come reviewer richiesto"]
    unknown = await _get(client, out["id"], "nessuno@example.test")
    assert _transition(unknown, "approve")["reasons"] == ["Accesso come reviewer richiesto"]
    owner_view = await _get(client, out["id"], actor["email"])
    assert _transition(owner_view, "approve")["allowed"] is False
    rev_view = await _get(client, out["id"], reviewer["email"])
    assert _transition(rev_view, "approve")["allowed"] is True
    assert _transition(rev_view, "request_changes")["allowed"] is True
    for action in ("approve", "request_changes"):
        resp = await _do(client, out, action, actor["email"], note="x")
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "not_reviewer"
    assert (await _get(client, out["id"]))["status"] == "in_review"


async def test_request_changes_requires_note_and_returns_to_draft(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    resp = await _do(client, out, "request_changes", reviewer["email"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "transition_not_allowed"
    assert resp.json()["detail"]["reasons"]
    resp = await _do(client, out, "request_changes", reviewer["email"], note="   ")
    assert resp.status_code == 409
    resp = await _do(client, out, "request_changes", reviewer["email"], note="Manca il caso KO")
    assert resp.status_code == 200, resp.text
    back = resp.json()
    assert back["status"] == "draft"
    assert back["working_revision"]["status"] == "draft"
    async with TestSession() as s:
        comments = (
            await s.execute(
                select(TlReviewComment).where(
                    TlReviewComment.revision_id == uuid.UUID(back["working_revision"]["id"])
                )
            )
        ).scalars().all()
    assert len(comments) == 1
    assert comments[0].body == "Manca il caso KO"
    assert comments[0].author == reviewer["email"]
    assert comments[0].scenario_id is None


async def test_approve_sets_designed_and_creates_snapshot(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    resp = await _do(client, out, "approve", reviewer["email"])
    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["status"] == "designed"
    rev = done["working_revision"]
    assert rev["status"] == "approved"
    assert rev["approved_by"] == reviewer["email"]
    assert rev["approved_at"] is not None

    hist = (await client.get(f"{BASE}/features/{done['id']}/history")).json()
    versions = [h for h in hist if h["kind"] == "version"]
    assert len(versions) == 1
    resp = await client.get(f"{BASE}/features/{done['id']}/versions/{versions[0]['version_id']}")
    assert resp.status_code == 200
    snap = resp.json()["snapshot"]
    sc = rev["scenarios"][0]
    expected = render_feature_file(
        RenderFeature(
            code=done["code"],
            feature_tag=feature_tag(scope["code"], done["number"]),
            title=rev["title"],
            description=rev["description"],
            background_steps=rev["background_steps"],
            scenarios=[
                RenderScenario(
                    tag=sc["tag"],
                    title=sc["title"],
                    kind=TlScenarioKind(sc["kind"]),
                    polarity=TlPolarity(sc["polarity"]),
                    extra_tags=sc["extra_tags"],
                    steps=sc["steps"],
                    examples=sc["examples"],
                    detail_link=sc["detail_link"],
                )
            ],
        )
    )
    assert snap["file"] == expected
    assert snap["file"].startswith(f"# qa-hub: {done['code']}\n# language: it\n")
    assert sc["tag"] in snap["file"]
    assert snap["scenarios"][0]["tag"] == sc["tag"]
    assert snap["scenarios"][0]["steps"] == VALID_STEPS
    assert snap["revision"]["number"] == 1
    assert snap["revision"]["approved_by"] == reviewer["email"]
    # versione di un'altra feature -> 404
    other = await _new_feature(client, scope, actor, title="Altra")
    resp = await client.get(f"{BASE}/features/{other['id']}/versions/{versions[0]['version_id']}")
    assert resp.status_code == 404


async def test_reopen_not_allowed_in_phase_1(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    assert _transition(detail, "reopen") == {
        "action": "reopen", "allowed": False, "reasons": [REASON_REOPEN],
    }
    resp = await _do(client, detail, "reopen", actor["email"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["reasons"] == [REASON_REOPEN]


async def test_history_lists_events_and_versions_desc(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    await _do(client, out, "approve", reviewer["email"])
    resp = await client.get(f"{BASE}/features/{detail['id']}/history")
    assert resp.status_code == 200
    items = resp.json()
    ats = [i["at"] for i in items]
    assert ats == sorted(ats, reverse=True)
    actions = [i.get("action") for i in items if i["kind"] == "event"]
    assert actions.count("tl.feature.created") == 1
    assert actions.count("tl.feature.updated") == 1  # un solo evento per la modifica
    assert "tl.revision.submitted" in actions
    assert "tl.revision.approved" in actions
    # la versione segue (cronologicamente) l'evento di approvazione: in desc viene prima
    kinds = [i["kind"] for i in items]
    assert kinds[0] == "version"
    assert items[1]["action"] == "tl.revision.approved"
    assert items[0]["actor"] == reviewer["email"]
    assert items[-1]["action"] == "tl.feature.created"
    assert items[-1]["actor"] == actor["email"]
    missing = await client.get(f"{BASE}/features/{uuid.uuid4()}/history")
    assert missing.status_code == 404


async def test_feature_status_unchanged_when_already_published(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    rev_id = uuid.UUID(detail["working_revision"]["id"])
    async with TestSession() as s:
        await s.execute(
            update(TlFeature)
            .where(TlFeature.id == uuid.UUID(detail["id"]))
            .values(published_revision_id=rev_id, status=TlFeatureStatus.IMPLEMENTED)
        )
        await s.commit()

    out = (await _do(client, detail, "submit", actor["email"])).json()
    assert out["working_revision"]["status"] == "in_review"
    assert out["status"] == "implemented"

    out = (await _do(client, out, "request_changes", reviewer["email"], note="Rivedi")).json()
    assert out["working_revision"]["status"] == "draft"
    assert out["status"] == "implemented"

    out = (await _do(client, out, "submit", actor["email"])).json()
    out = (await _do(client, out, "approve", reviewer["email"])).json()
    assert out["working_revision"]["status"] == "approved"
    assert out["status"] == "implemented"


async def test_second_approve_is_blocked_and_single_version(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    assert (await _do(client, out, "approve", reviewer["email"])).status_code == 200
    resp = await _do(client, out, "approve", reviewer["email"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "transition_not_allowed"
    async with TestSession() as s:
        versions = (
            await s.execute(
                select(TlFeatureVersion).where(
                    TlFeatureVersion.feature_id == uuid.UUID(detail["id"])
                )
            )
        ).scalars().all()
    assert len(versions) == 1


async def test_submit_on_non_draft_revision_returns_409(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    resp = await _do(client, out, "submit", actor["email"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "transition_not_allowed"


async def test_approve_on_draft_revision_returns_409(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    resp = await _do(client, detail, "approve", reviewer["email"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "transition_not_allowed"


async def test_transition_with_unknown_actor_returns_403(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    resp = await _do(client, detail, "submit", "nessuno@example.test")
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "unknown_actor"


# ── ritorno indietro: withdraw_approval (approved -> in_review) e withdraw (in_review -> draft) ──

NOT_APPROVED = "La revisione non è approvata"
NOT_IN_REVIEW = "La revisione non è in revisione"
NOT_REVIEWER = "Solo il reviewer assegnato può eseguire questa azione"
NOT_OWNER = "Solo l'owner di progettazione può eseguire questa azione"
PR_MERGED = "La PR è già stata mergiata: usa Riapri"


@pytest.fixture
def fake_git(monkeypatch):
    monkeypatch.setattr(settings, "tl_hub_base_url", "https://hub.test")
    monkeypatch.setattr(settings, "tl_base_branch", "main")
    return install_fake_git(monkeypatch, FakeGitClient())


async def _post_transition(client, feature_id, revision_id, action, email, note=None):
    body = {"action": action, "actor_email": email}
    if note is not None:
        body["note"] = note
    return await client.post(
        f"{BASE}/features/{feature_id}/revisions/{revision_id}/transitions", json=body
    )


async def _approved_feature(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    resp = await _do(client, out, "approve", reviewer["email"])
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _version_count(feature_id):
    async with TestSession() as s:
        rows = (
            await s.execute(
                select(TlFeatureVersion).where(TlFeatureVersion.feature_id == uuid.UUID(feature_id))
            )
        ).scalars().all()
    return len(rows)


async def _event_actions(client, feature_id):
    hist = (await client.get(f"{BASE}/features/{feature_id}/history")).json()
    return [i.get("action") for i in hist if i["kind"] == "event"]


async def test_transitions_append_the_two_withdraw_actions(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    assert [t["action"] for t in detail["transitions"]] == [
        "submit", "approve", "request_changes", "reopen", "withdraw_approval", "withdraw",
    ]


async def test_withdraw_approval_returns_revision_to_review(
    client, scope, actor, reviewer, fake_git
):
    done = await _approved_feature(client, scope, actor, reviewer)
    assert done["working_revision"]["pr_number"] == 101
    resp = await _do(client, done, "withdraw_approval", reviewer["email"])
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "in_review"
    rev = out["working_revision"]
    assert rev["status"] == "in_review"
    assert rev["approved_by"] is None and rev["approved_at"] is None
    # PR, Jira keys and the approval snapshot are untouched
    assert rev["pr_number"] == 101 and rev["pr_state"] == "open"
    assert out["jira_impl_key"] == done["jira_impl_key"]
    assert await _version_count(done["id"]) == 1
    assert "tl.revision.approval_withdrawn" in await _event_actions(client, done["id"])


async def test_withdraw_approval_by_non_reviewer_is_forbidden(
    client, scope, actor, reviewer, fake_git
):
    done = await _approved_feature(client, scope, actor, reviewer)
    owner_view = await _get(client, done["id"], actor["email"])
    check = _transition(owner_view, "withdraw_approval")
    assert check["allowed"] is False and check["reasons"] == [NOT_REVIEWER]
    reviewer_view = await _get(client, done["id"], reviewer["email"])
    assert _transition(reviewer_view, "withdraw_approval")["allowed"] is True
    resp = await _do(client, done, "withdraw_approval", actor["email"])
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "not_reviewer"
    assert (await _get(client, done["id"]))["working_revision"]["status"] == "approved"


async def test_withdraw_approval_blocked_when_pr_merged(client, scope, actor, reviewer, fake_git):
    done = await _approved_feature(client, scope, actor, reviewer)
    rev_id = done["working_revision"]["id"]
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == uuid.UUID(rev_id))
            .values(status=TlRevisionStatus.MERGED, pr_state="merged")
        )
        await s.commit()
    resp = await _post_transition(
        client, done["id"], rev_id, "withdraw_approval", reviewer["email"]
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "transition_not_allowed"
    assert PR_MERGED in resp.json()["detail"]["reasons"]


async def test_withdraw_approval_requires_an_approved_revision(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    resp = await _do(client, out, "withdraw_approval", reviewer["email"])
    assert resp.status_code == 409
    assert NOT_APPROVED in resp.json()["detail"]["reasons"]


async def test_withdraw_approval_keeps_feature_status_when_already_published(
    client, scope, actor, reviewer
):
    detail = await _ready_feature(client, scope, actor, reviewer)
    rev_id = uuid.UUID(detail["working_revision"]["id"])
    async with TestSession() as s:
        await s.execute(
            update(TlFeature)
            .where(TlFeature.id == uuid.UUID(detail["id"]))
            .values(published_revision_id=rev_id, status=TlFeatureStatus.IMPLEMENTED)
        )
        await s.commit()
    out = (await _do(client, detail, "submit", actor["email"])).json()
    out = (await _do(client, out, "approve", reviewer["email"])).json()
    assert out["status"] == "implemented"
    out = (await _do(client, out, "withdraw_approval", reviewer["email"])).json()
    assert out["working_revision"]["status"] == "in_review"
    assert out["status"] == "implemented"


async def test_withdraw_returns_in_review_to_draft_by_owner_without_note(
    client, scope, actor, reviewer
):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    resp = await _do(client, out, "withdraw", actor["email"])
    assert resp.status_code == 200, resp.text
    back = resp.json()
    assert back["status"] == "draft"
    assert back["working_revision"]["status"] == "draft"
    async with TestSession() as s:
        comments = (
            await s.execute(
                select(TlReviewComment).where(
                    TlReviewComment.revision_id == uuid.UUID(back["working_revision"]["id"])
                )
            )
        ).scalars().all()
    assert comments == []
    assert "tl.revision.withdrawn" in await _event_actions(client, detail["id"])


async def test_withdraw_by_non_owner_is_forbidden(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    reviewer_view = await _get(client, out["id"], reviewer["email"])
    check = _transition(reviewer_view, "withdraw")
    assert check["allowed"] is False and check["reasons"] == [NOT_OWNER]
    assert _transition(await _get(client, out["id"], actor["email"]), "withdraw")["allowed"] is True
    resp = await _do(client, out, "withdraw", reviewer["email"])
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "not_owner"
    assert (await _get(client, out["id"]))["working_revision"]["status"] == "in_review"


async def test_withdraw_requires_an_in_review_revision(client, scope, actor, reviewer, fake_git):
    detail = await _ready_feature(client, scope, actor, reviewer)
    resp = await _do(client, detail, "withdraw", actor["email"])
    assert resp.status_code == 409
    assert NOT_IN_REVIEW in resp.json()["detail"]["reasons"]
    out = (await _do(client, detail, "submit", actor["email"])).json()
    approved = (await _do(client, out, "approve", reviewer["email"])).json()
    resp = await _do(client, approved, "withdraw", actor["email"])
    assert resp.status_code == 409
    assert NOT_IN_REVIEW in resp.json()["detail"]["reasons"]


async def test_second_withdraw_call_returns_409_not_500(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    out = (await _do(client, detail, "submit", actor["email"])).json()
    assert (await _do(client, out, "withdraw", actor["email"])).status_code == 200
    resp = await _do(client, out, "withdraw", actor["email"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "transition_not_allowed"


async def test_reapproval_after_withdrawal_creates_second_snapshot_and_reuses_the_pr(
    client, scope, actor, reviewer, fake_git
):
    detail = await _ready_feature(client, scope, actor, reviewer)
    async with TestSession() as s:
        await s.execute(
            update(TlFeature)
            .where(TlFeature.id == uuid.UUID(detail["id"]))
            .values(jira_impl_key="PQ-77")
        )
        await s.commit()
    out = (await _do(client, detail, "submit", actor["email"])).json()
    done = (await _do(client, out, "approve", reviewer["email"])).json()
    out = (await _do(client, done, "withdraw_approval", reviewer["email"])).json()
    again = await _do(client, out, "approve", reviewer["email"])
    assert again.status_code == 200, again.text
    final = again.json()
    assert final["working_revision"]["status"] == "approved"
    assert final["working_revision"]["pr_number"] == 101
    assert len(fake_git.calls_of("create_pull")) == 1
    assert await _version_count(done["id"]) == 2
    # the implementation task exists already: no new creation attempt (and so no Jira error)
    assert final["jira_impl_key"] == "PQ-77"
    assert final["jira_error"] is None


async def test_reapproval_does_not_open_a_second_pr_when_the_first_was_merged_before_the_sync(
    client, scope, actor, reviewer, fake_git
):
    done = await _approved_feature(client, scope, actor, reviewer)
    assert done["working_revision"]["pr_number"] == 101
    out = (await _do(client, done, "withdraw_approval", reviewer["email"])).json()
    # merged on GitHub, but the sync has not run yet: the hub still thinks the PR is open
    fake_git.set_pull_state(101, "merged", "abc123")
    pushes = len(fake_git.calls_of("put_file"))

    again = await _do(client, out, "approve", reviewer["email"])
    assert again.status_code == 200, again.text
    rev = again.json()["working_revision"]
    assert rev["pr_number"] == 101
    assert rev["pr_error"] is None
    assert len(fake_git.calls_of("create_pull")) == 1
    assert len(fake_git.calls_of("put_file")) == pushes
    assert "tl.pr.updated" not in await _event_actions(client, done["id"])


async def test_withdraw_approval_is_refused_while_the_pr_is_being_published(
    client, scope, actor, reviewer, fake_git
):
    done = await _approved_feature(client, scope, actor, reviewer)
    rev_id = done["working_revision"]["id"]
    async with TestSession() as s, try_advisory_lock(s, f"tl_pr:{rev_id}") as got:
        assert got
        resp = await _do(client, done, "withdraw_approval", reviewer["email"])
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "transition_not_allowed"
        reasons = resp.json()["detail"]["reasons"]
        assert "Pubblicazione della PR in corso: riprova tra poco" in reasons
    # the revision is untouched and the same call works once the publication is over
    assert (await _get(client, done["id"]))["working_revision"]["status"] == "approved"
    assert (await _do(client, done, "withdraw_approval", reviewer["email"])).status_code == 200


async def test_withdraw_approval_with_a_merged_pr_is_a_409_even_for_the_wrong_actor(
    client, scope, actor, reviewer, fake_git
):
    done = await _approved_feature(client, scope, actor, reviewer)
    rev_id = done["working_revision"]["id"]
    async with TestSession() as s:  # status still approved, only the PR state says merged
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == uuid.UUID(rev_id))
            .values(pr_state="merged")
        )
        await s.commit()
    resp = await _post_transition(client, done["id"], rev_id, "withdraw_approval", actor["email"])
    assert resp.status_code == 409
    assert PR_MERGED in resp.json()["detail"]["reasons"]


async def test_reapproval_after_edits_updates_the_same_pr(client, scope, actor, reviewer, fake_git):
    done = await _approved_feature(client, scope, actor, reviewer)
    out = (await _do(client, done, "withdraw_approval", reviewer["email"])).json()
    out = (await _do(client, out, "withdraw", actor["email"])).json()
    rev = out["working_revision"]
    resp = await client.patch(
        f"{BASE}/features/{out['id']}/revisions/{rev['id']}",
        json={
            "lock_version": rev["lock_version"],
            "title": "Titolo corretto",
            "actor_email": actor["email"],
        },
    )
    assert resp.status_code == 200, resp.text
    pushes = len(fake_git.calls_of("put_file"))

    ready = await _get(client, out["id"], actor["email"])
    out = (await _do(client, ready, "submit", actor["email"])).json()
    final = (await _do(client, out, "approve", reviewer["email"])).json()
    assert final["working_revision"]["pr_number"] == 101
    assert len(fake_git.calls_of("create_pull")) == 1
    assert len(fake_git.calls_of("put_file")) > pushes
    assert "Titolo corretto" in fake_git.calls_of("put_file")[-1][2]
    assert "tl.pr.updated" in await _event_actions(client, done["id"])


async def test_withdraw_approval_on_a_reopened_feature_keeps_the_published_revision(
    client, scope, actor, reviewer, fake_git
):
    done = await _approved_feature(client, scope, actor, reviewer)
    first_id = uuid.UUID(done["working_revision"]["id"])
    # simulate the merge: first revision published, nothing in progress
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == first_id)
            .values(status=TlRevisionStatus.MERGED, pr_state="merged")
        )
        await s.execute(
            update(TlFeature)
            .where(TlFeature.id == uuid.UUID(done["id"]))
            .values(
                published_revision_id=first_id,
                working_revision_id=None,
                status=TlFeatureStatus.IMPLEMENTED,
            )
        )
        await s.commit()
    detail = await _get(client, done["id"], actor["email"])
    resp = await client.post(
        f"{BASE}/features/{done['id']}/revisions/{first_id}/transitions",
        json={"action": "reopen", "actor_email": actor["email"]},
    )
    assert resp.status_code == 200, resp.text
    reopened = resp.json()
    second = reopened["working_revision"]
    assert second["id"] != str(first_id) and second["status"] == "draft"
    assert detail["published_revision"]["id"] == str(first_id)
    resp = await client.patch(  # a reopened revision needs its own design task
        f"{BASE}/features/{done['id']}",
        json={"jira_design_key": "PQ-20", "actor_email": actor["email"]},
    )
    assert resp.status_code == 200, resp.text
    reopened = await _get(client, done["id"], actor["email"])

    out = (await _do(client, reopened, "submit", actor["email"])).json()
    out = (await _do(client, out, "approve", reviewer["email"])).json()
    out = (await _do(client, out, "withdraw_approval", reviewer["email"])).json()
    assert out["working_revision"]["status"] == "in_review"
    assert out["status"] == "implemented"
    assert out["published_revision"]["id"] == str(first_id)

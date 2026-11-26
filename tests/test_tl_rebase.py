# ruff: noqa: S101
"""Riapertura e riallineamento a tre vie (fase 2, Task 9)."""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.test_lifecycle import TlFeatureRevision, TlRevisionStatus, TlScenarioContent
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _use_test_db,
    actor,
    scope,
)
from tests.test_tl_git_import import (  # noqa: F401
    PR,
    _events,
    _import,
    _main_text,
    _merged,
    fake,
)
from tests.test_tl_workflow import _do, _get, reviewer  # noqa: F401


async def _reopen(client, fid, rev_id, email):
    url = f"{BASE}/features/{fid}/revisions/{rev_id}/transitions"
    return await client.post(url, json={"action": "reopen", "actor_email": email})


async def _reopened(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    published = (await _get(client, done["id"], actor["email"]))["published_revision"]
    resp = await _reopen(client, done["id"], published["id"], actor["email"])
    assert resp.status_code == 200, resp.text
    # a reopened revision needs its own design task before it can be submitted again
    resp = await client.patch(
        f"{BASE}/features/{done['id']}",
        json={"jira_design_key": "PQ-20", "actor_email": actor["email"]},
    )
    assert resp.status_code == 200, resp.text
    return done, await _get(client, done["id"], actor["email"])


async def _edit_draft_steps(client, detail, steps, email):
    rev = detail["working_revision"]
    scen = rev["scenarios"][0]
    url = f"{BASE}/features/{detail['id']}/revisions/{rev['id']}/scenarios/{scen['id']}"
    resp = await client.patch(
        url, json={"lock_version": rev["lock_version"], "steps": steps, "actor_email": email}
    )
    assert resp.status_code == 200, resp.text
    return await _get(client, detail["id"], email)


async def _git_edit(fake, done, old, new):
    text = _main_text(fake, done).replace(old, new)
    sha = fake.seed_file(done["git_path"], text)
    await _import(fake, done["git_path"], sha)


async def _stale_conflict(client, scope, actor, reviewer, fake):
    """Bozza riaperta e Git modificano lo stesso scenario -> conflitto."""
    done, detail = await _reopened(client, scope, actor, reviewer, fake)
    detail = await _edit_draft_steps(
        client, detail,
        "Dato un utente registrato\nQuando paga con la carta\nAllora il pagamento è confermato",
        actor["email"],
    )
    await _git_edit(fake, done, "Quando paga", "Quando paga con il bonifico")
    return done, await _get(client, done["id"], actor["email"])


def _rebase_url(detail):
    return f"{BASE}/features/{detail['id']}/revisions/{detail['working_revision']['id']}/rebase"


async def test_reopen_copies_published_content(client, scope, actor, reviewer, fake):
    done, detail = await _reopened(client, scope, actor, reviewer, fake)
    work, pub = detail["working_revision"], detail["published_revision"]
    assert work["status"] == "draft"
    assert work["number"] == pub["number"] + 1
    assert work["title"] == pub["title"]
    assert [s["tag"] for s in work["scenarios"]] == [s["tag"] for s in pub["scenarios"]]
    assert work["scenarios"][0]["steps"] == pub["scenarios"][0]["steps"]
    assert detail["status"] == "implemented"  # la feature resta nel suo stato
    async with TestSession() as s:
        rev = await s.get(TlFeatureRevision, uuid.UUID(work["id"]))
        assert str(rev.base_revision_id) == pub["id"]
    assert len(await _events(done["id"], "tl.revision.reopened")) == 1


async def test_reopen_blocked_with_open_revision(client, scope, actor, reviewer, fake):
    done, detail = await _reopened(client, scope, actor, reviewer, fake)
    reopen = next(t for t in detail["transitions"] if t["action"] == "reopen")
    assert reopen["allowed"] is False
    assert reopen["reasons"] == ["Esiste già una revisione in corso"]
    resp = await _reopen(client, done["id"], detail["published_revision"]["id"], actor["email"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["reasons"] == ["Esiste già una revisione in corso"]


async def test_submit_blocked_when_stale(client, scope, actor, reviewer, fake):
    _, detail = await _stale_conflict(client, scope, actor, reviewer, fake)
    assert detail["working_revision"]["base_stale"] is True
    submit = next(t for t in detail["transitions"] if t["action"] == "submit")
    assert submit["allowed"] is False
    assert "La bozza è basata su una versione superata: riallineala" in submit["reasons"]


async def test_rebase_view_marks_conflicts(client, scope, actor, reviewer, fake):
    _, detail = await _stale_conflict(client, scope, actor, reviewer, fake)
    resp = await client.get(_rebase_url(detail))
    assert resp.status_code == 200, resp.text
    view = resp.json()
    [sc] = view["scenarios"]
    assert sc["conflict"] is True
    assert "carta" in sc["draft"]["steps"]
    assert "bonifico" in sc["git"]["steps"]
    assert "Quando paga\n" in sc["base"]["steps"] + "\n"
    fields = {m["field"]: m for m in view["metadata"]}
    assert set(fields) == {"title", "description", "background"}
    assert not any(m["conflict"] for m in view["metadata"])


async def test_rebase_requires_choice_for_conflicts(client, scope, actor, reviewer, fake):
    _, detail = await _stale_conflict(client, scope, actor, reviewer, fake)
    rev = detail["working_revision"]
    resp = await client.post(
        _rebase_url(detail),
        json={"lock_version": rev["lock_version"], "actor_email": actor["email"], "choices": {}},
    )
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["code"] == "rebase_required"
    assert body["conflicts"] == [rev["scenarios"][0]["id"]]


async def test_rebase_applies_choices_and_clears_stale(client, scope, actor, reviewer, fake):
    done, detail = await _stale_conflict(client, scope, actor, reviewer, fake)
    rev = detail["working_revision"]
    sid = rev["scenarios"][0]["id"]
    resp = await client.post(
        _rebase_url(detail),
        json={
            "lock_version": rev["lock_version"],
            "actor_email": actor["email"],
            "choices": {sid: "git"},
        },
    )
    assert resp.status_code == 200, resp.text
    after = (await _get(client, done["id"], actor["email"]))["working_revision"]
    assert after["base_stale"] is False
    assert after["lock_version"] == rev["lock_version"] + 1
    assert "bonifico" in after["scenarios"][0]["steps"]
    async with TestSession() as s:
        db_rev = await s.get(TlFeatureRevision, uuid.UUID(after["id"]))
        published = (await _get(client, done["id"]))["published_revision"]
        assert str(db_rev.base_revision_id) == published["id"]
    assert len(await _events(done["id"], "tl.revision.rebased")) == 1
    submit = next(t for t in (await _get(client, done["id"], actor["email"]))["transitions"]
                  if t["action"] == "submit")
    assert "La bozza è basata su una versione superata: riallineala" not in submit["reasons"]


async def test_rebase_takes_only_changed_side_without_conflict(
    client, scope, actor, reviewer, fake
):
    """Solo Git cambia lo scenario: preso da Git senza scelta; la bozza non perde nulla."""
    done, detail = await _reopened(client, scope, actor, reviewer, fake)
    await _git_edit(fake, done, "Quando paga", "Quando paga online")
    detail = await _get(client, done["id"], actor["email"])
    rev = detail["working_revision"]
    resp = await client.post(
        _rebase_url(detail),
        json={"lock_version": rev["lock_version"], "actor_email": actor["email"], "choices": {}},
    )
    assert resp.status_code == 200, resp.text
    after = (await _get(client, done["id"], actor["email"]))["working_revision"]
    assert "online" in after["scenarios"][0]["steps"]


async def test_rebase_stale_lock_returns_409(client, scope, actor, reviewer, fake):
    _, detail = await _stale_conflict(client, scope, actor, reviewer, fake)
    rev = detail["working_revision"]
    resp = await client.post(
        _rebase_url(detail),
        json={
            "lock_version": rev["lock_version"] - 1,
            "actor_email": actor["email"],
            "choices": {rev["scenarios"][0]["id"]: "draft"},
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "stale_lock"


async def test_rebase_not_needed_returns_409(client, scope, actor, reviewer, fake):
    _, detail = await _reopened(client, scope, actor, reviewer, fake)
    rev = detail["working_revision"]
    resp = await client.post(
        _rebase_url(detail),
        json={"lock_version": rev["lock_version"], "actor_email": actor["email"], "choices": {}},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "rebase_not_needed"


async def test_reapproval_updates_same_pr_branch(client, scope, actor, reviewer, fake):
    done, detail = await _reopened(client, scope, actor, reviewer, fake)
    detail = await _edit_draft_steps(
        client, detail,
        "Dato un utente registrato\nQuando paga con la carta\nAllora il pagamento è confermato",
        actor["email"],
    )
    detail = (await _do(client, detail, "submit", actor["email"])).json()
    detail = (await _do(client, detail, "approve", reviewer["email"])).json()
    r2 = detail["working_revision"]
    assert r2["pr_state"] == "open"
    pr_number = r2["pr_number"]
    branch = f"qa-hub/{done['code']}-r{r2['number']}"
    # main cambia altrove (Contesto): r2 approvata diventa bozza superata, la PR resta
    text = _main_text(fake, done).replace("Allora il pagamento è confermato",
                                          "Allora il pagamento è registrato")
    sha = fake.seed_file(done["git_path"], text)
    await _import(fake, done["git_path"], sha)
    detail = await _get(client, done["id"], actor["email"])
    r2 = detail["working_revision"]
    assert r2["status"] == "draft" and r2["base_stale"] is True and r2["pr_state"] == "open"
    view = (await client.get(_rebase_url(detail))).json()
    choices = {sc["scenario_id"]: "draft" for sc in view["scenarios"] if sc["conflict"]}
    resp = await client.post(
        _rebase_url(detail),
        json={"lock_version": r2["lock_version"], "actor_email": actor["email"],
              "choices": choices},
    )
    assert resp.status_code == 200, resp.text
    detail = await _get(client, done["id"], actor["email"])
    detail = (await _do(client, detail, "submit", actor["email"])).json()
    resp = await _do(client, detail, "approve", reviewer["email"])
    assert resp.status_code == 200, resp.text
    r2 = resp.json()["working_revision"]
    assert r2["pr_number"] == pr_number
    assert r2["pr_state"] == "open" and r2["pr_error"] is None
    assert len([c for c in fake.calls_of("create_pull") if c[1] == branch]) == 1
    assert fake.files(branch)[done["git_path"]][0].count("con la carta") == 1
    assert fake.branches[branch] != fake.branches["main"]
    assert fake.calls_of("reset_branch") == []  # a working branch is never reset
    async with TestSession() as s:
        rows = (
            await s.execute(
                select(TlScenarioContent).where(
                    TlScenarioContent.revision_id == uuid.UUID(r2["id"])
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        rev = await s.get(TlFeatureRevision, uuid.UUID(r2["id"]))
        assert rev.status == TlRevisionStatus.APPROVED

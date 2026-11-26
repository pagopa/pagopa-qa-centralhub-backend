# ruff: noqa: S101
"""Jira keys per revision and Jira history (design 2026-11-26)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, text, update

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureStatus,
    TlRevisionStatus,
    TlScope,
)
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    BASE,
    _use_test_db,
    actor,
    scope,
)
from tests.test_tl_git_import import _merged, fake  # noqa: F401
from tests.test_tl_jira_tasks import _design, _jira_scope, jira  # noqa: F401
from tests.test_tl_rebase import _git_edit
from tests.test_tl_workflow import _do, _get, _ready_feature, reviewer  # noqa: F401


async def _feature_with_revisions(session, numbers=(1,), **feature_keys):
    scope = TlScope(code="H" + uuid.uuid4().hex[:8], name="History")
    session.add(scope)
    await session.flush()
    feature = TlFeature(
        scope_id=scope.id, number=1, code=f"FEAT_001_{scope.code}",
        status=TlFeatureStatus.IMPLEMENTED, created_by="t@example.test", **feature_keys,
    )
    session.add(feature)
    await session.flush()
    revisions = []
    for n in numbers:
        rev = TlFeatureRevision(
            feature_id=feature.id, number=n, status=TlRevisionStatus.MERGED,
            title="T", created_by="t@example.test",
        )
        session.add(rev)
        revisions.append(rev)
    await session.flush()
    return scope, feature, revisions


async def test_revision_has_jira_key_columns():
    async with TestSession() as s:
        scope, _, (rev,) = await _feature_with_revisions(s)
        rev.jira_design_key, rev.jira_impl_key, rev.jira_qa_epic_key = "PQ-1", "ABC-2", "PQ-3"
        await s.commit()
        try:
            async with TestSession() as other:  # fresh session: proves the columns are persisted
                loaded = await other.get(TlFeatureRevision, rev.id)
                assert (loaded.jira_design_key, loaded.jira_impl_key, loaded.jira_qa_epic_key) == (
                    "PQ-1", "ABC-2", "PQ-3",
                )
        finally:
            await s.execute(delete(TlScope).where(TlScope.id == scope.id))
            await s.commit()


async def test_backfill_assigns_first_and_working_revision():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parent.parent / "alembic" / "versions" / "0043_revision_jira_keys.py"
    spec = importlib.util.spec_from_file_location("mig_0043", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backfill = module.BACKFILL_SQL
    async with TestSession() as s:
        scope, feature, (r1, r2) = await _feature_with_revisions(
            s, numbers=(1, 2), jira_design_key="PQ-1", jira_impl_key="ABC-2"
        )
        feature.working_revision_id = r2.id
        scope2, bare, (b1,) = await _feature_with_revisions(s)
        await s.commit()
        try:
            await s.execute(text(backfill))
            await s.execute(text(backfill))  # idempotent
            await s.commit()
            async with TestSession() as other:
                first = await other.get(TlFeatureRevision, r1.id)
                working = await other.get(TlFeatureRevision, r2.id)
                untouched = await other.get(TlFeatureRevision, b1.id)
                assert (first.jira_design_key, first.jira_impl_key) == ("PQ-1", "ABC-2")
                assert (working.jira_design_key, working.jira_impl_key) == ("PQ-1", "ABC-2")
                assert untouched.jira_design_key is None and untouched.jira_impl_key is None
        finally:
            await s.execute(delete(TlScope).where(TlScope.id.in_([scope.id, scope2.id])))
            await s.commit()


REUSED = "Il task di progettazione è già stato usato da un'altra revisione: serve un nuovo task"


async def _revisions(fid):
    async with TestSession() as s:
        rows = (
            await s.execute(
                select(TlFeatureRevision)
                .where(TlFeatureRevision.feature_id == uuid.UUID(fid))
                .order_by(TlFeatureRevision.number)
            )
        ).scalars().all()
        return rows


async def _feature_row(fid):
    async with TestSession() as s:
        return await s.get(TlFeature, uuid.UUID(fid))


async def _published(client, scope, actor, reviewer):
    """Implemented feature (design PQ-12, epic PQ-495) built straight in the DB after approval."""
    detail = await _ready_feature(client, scope, actor, reviewer)
    resp = await client.patch(
        f"{BASE}/features/{detail['id']}",
        json={"jira_qa_epic_key": "PQ-495", "actor_email": actor["email"]},
    )
    assert resp.status_code == 200, resp.text
    detail = (await _do(client, detail, "submit", actor["email"])).json()
    detail = (await _do(client, detail, "approve", reviewer["email"])).json()
    rev_id = uuid.UUID(detail["working_revision"]["id"])
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision).where(TlFeatureRevision.id == rev_id)
            .values(status=TlRevisionStatus.MERGED)
        )
        await s.execute(
            update(TlFeature).where(TlFeature.id == uuid.UUID(detail["id"])).values(
                status=TlFeatureStatus.IMPLEMENTED, published_revision_id=rev_id,
                working_revision_id=None,
            )
        )
        await s.commit()
    return await _get(client, detail["id"], actor["email"])


async def _reopen(client, detail, actor):
    rev = detail["published_revision"]["id"]
    resp = await client.post(
        f"{BASE}/features/{detail['id']}/revisions/{rev}/transitions",
        json={"action": "reopen", "actor_email": actor["email"]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_first_draft_writes_keys_to_working_revision(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    [rev] = await _revisions(detail["id"])
    assert rev.jira_design_key == "PQ-12"


async def test_reopen_clears_design_and_impl_keeps_epic(client, scope, actor, reviewer):
    done = await _published(client, scope, actor, reviewer)
    reopened = await _reopen(client, done, actor)
    assert reopened["jira_design_key"] is None and reopened["jira_impl_key"] is None
    assert reopened["jira_qa_epic_key"] == "PQ-495"
    rev1, rev2 = await _revisions(done["id"])
    assert rev1.jira_design_key == "PQ-12" and rev1.jira_qa_epic_key == "PQ-495"
    assert rev2.jira_design_key is None and rev2.jira_impl_key is None
    assert rev2.jira_qa_epic_key == "PQ-495"


async def test_update_feature_writes_keys_to_working_revision(client, scope, actor, reviewer):
    done = await _published(client, scope, actor, reviewer)
    await _reopen(client, done, actor)
    resp = await client.patch(
        f"{BASE}/features/{done['id']}",
        json={"jira_design_key": "PQ-20", "jira_qa_epic_key": "PQ-496", "actor_email": actor["email"]},
    )
    assert resp.status_code == 200, resp.text
    rev1, rev2 = await _revisions(done["id"])
    assert (rev1.jira_design_key, rev1.jira_qa_epic_key) == ("PQ-12", "PQ-495")
    assert (rev2.jira_design_key, rev2.jira_qa_epic_key) == ("PQ-20", "PQ-496")
    row = await _feature_row(done["id"])
    assert (row.jira_design_key, row.jira_qa_epic_key) == ("PQ-20", "PQ-496")


async def test_update_rejects_reused_design_key(client, scope, actor, reviewer):
    done = await _published(client, scope, actor, reviewer)
    await _reopen(client, done, actor)
    resp = await client.patch(
        f"{BASE}/features/{done['id']}",
        json={"jira_design_key": "PQ-12", "actor_email": actor["email"]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "jira_design_key_reused"
    assert resp.json()["detail"]["message"] == REUSED


async def test_submit_blocked_when_design_key_reused(client, scope, actor, reviewer):
    done = await _published(client, scope, actor, reviewer)
    reopened = await _reopen(client, done, actor)
    rev2_id = uuid.UUID(reopened["working_revision"]["id"])
    async with TestSession() as s:  # state left by the old model: key shared with Rev. 1
        await s.execute(
            update(TlFeatureRevision).where(TlFeatureRevision.id == rev2_id)
            .values(jira_design_key="PQ-12")
        )
        await s.execute(
            update(TlFeature).where(TlFeature.id == uuid.UUID(done["id"]))
            .values(jira_design_key="PQ-12")
        )
        await s.commit()
    detail = await _get(client, done["id"], actor["email"])
    submit = next(t for t in detail["transitions"] if t["action"] == "submit")
    assert submit["allowed"] is False
    assert REUSED in submit["reasons"]


async def test_create_design_task_writes_key_to_revision_with_revision_in_summary(
    client, scope, actor, reviewer, jira
):
    await _jira_scope(client, scope, jira)
    done = await _published(client, scope, actor, reviewer)
    before = len(jira.created)  # Rev. 1 already got its implementation task on approval
    await _reopen(client, done, actor)
    resp = await _design(client, done, actor)
    assert resp.status_code == 200, resp.text
    [created] = jira.created[before:]
    assert created["summary"].endswith("(Rev. 2)")
    _, rev2 = await _revisions(done["id"])
    assert rev2.jira_design_key == created["key"]
    assert (await _feature_row(done["id"])).jira_design_key == created["key"]


async def test_impl_task_created_for_reopened_revision(client, scope, actor, reviewer, jira):
    await _jira_scope(client, scope, jira)
    jira.add_issue("PQ-20", "Modifica", issue_type="Progettazione Scenari e casi di test")
    done = await _published(client, scope, actor, reviewer)
    before = len(jira.created)
    reopened = await _reopen(client, done, actor)
    resp = await client.patch(
        f"{BASE}/features/{done['id']}",
        json={"jira_design_key": "PQ-20", "actor_email": actor["email"]},
    )
    assert resp.status_code == 200, resp.text
    reopened = await _get(client, done["id"], actor["email"])
    submitted = (await _do(client, reopened, "submit", actor["email"])).json()
    approved = await _do(client, submitted, "approve", reviewer["email"])
    assert approved.status_code == 200, approved.text
    impl = [c for c in jira.created[before:] if c["issue_type"] == "Implementazione suite di TA"]
    assert len(impl) == 1
    assert impl[0]["summary"].endswith("(Rev. 2)")
    _, rev2 = await _revisions(done["id"])
    assert rev2.jira_impl_key == impl[0]["key"]


async def _reopened_with_new_design(client, scope, actor, reviewer, jira):
    await _jira_scope(client, scope, jira)
    jira.add_issue("PQ-20", "Modifica", issue_type="Progettazione Scenari e casi di test")
    done = await _published(client, scope, actor, reviewer)
    await _reopen(client, done, actor)
    resp = await client.patch(
        f"{BASE}/features/{done['id']}",
        json={"jira_design_key": "PQ-20", "actor_email": actor["email"]},
    )
    assert resp.status_code == 200, resp.text
    return done, resp.json()


async def test_detail_jira_history_lists_revisions_newest_first(client, scope, actor, reviewer, jira):
    _, detail = await _reopened_with_new_design(client, scope, actor, reviewer, jira)
    rows = detail["jira_history"]
    assert [r["revision_number"] for r in rows] == [2, 1]
    assert [r["is_current"] for r in rows] == [True, False]
    assert rows[0]["revision_status"] == "draft" and rows[1]["revision_status"] == "merged"
    assert rows[0]["design"]["key"] == "PQ-20" and rows[0]["design"]["role"] == "design"
    assert rows[0]["impl"] is None
    assert rows[1]["design"]["key"] == "PQ-12"
    assert rows[1]["impl"] is not None  # created on approval of Rev. 1
    assert rows[0]["qa_epic"]["key"] == "PQ-495" and rows[1]["qa_epic"]["key"] == "PQ-495"


async def test_history_epic_falls_back_to_scope_epic(client, scope, actor, reviewer, jira):
    await _jira_scope(client, scope, jira)
    detail = await _ready_feature(client, scope, actor, reviewer)  # no epic override
    rows = (await _get(client, detail["id"], actor["email"]))["jira_history"]
    assert [r["qa_epic"]["key"] for r in rows] == ["PQ-495"]


async def test_linked_keys_include_all_revisions(client, scope, actor, reviewer, jira):
    from app.services.test_lifecycle import jira_tasks

    done, _ = await _reopened_with_new_design(client, scope, actor, reviewer, jira)
    async with TestSession() as s:
        feature = await s.get(TlFeature, uuid.UUID(done["id"]))
        keys = await jira_tasks.linked_keys(s, feature)
    assert {"PQ-12", "PQ-20", "PQ-495"} <= set(keys)


async def test_delete_preview_includes_older_revision_issues(client, scope, actor, reviewer, jira):
    done, _ = await _reopened_with_new_design(client, scope, actor, reviewer, jira)
    data = (await client.get(f"{BASE}/features/{done['id']}/delete-preview")).json()
    keys = {i["key"] for i in data["jira_issues"]}
    assert {"PQ-12", "PQ-20"} <= keys


# ── Jira URLs accepted in key fields ──────────────────────────────────────────

URL = "https://pagopa.atlassian.net/browse/{key}"


@pytest.mark.parametrize(
    ("model", "field", "key"),
    [
        ("TlFeatureUpdate", "jira_design_key", "PQ-123"),
        ("TlFeatureUpdate", "jira_impl_key", "ABC-9"),
        ("TlFeatureUpdate", "jira_qa_epic_key", "PQ-495"),
        ("TlRevisionUpdate", "product_issue_key", "CHK-7"),
    ],
)
def test_key_fields_accept_a_jira_url(model, field, key):
    from app.schemas import test_lifecycle as schemas

    cls = getattr(schemas, model)
    kwargs = {"lock_version": 0} if model == "TlRevisionUpdate" else {}
    for raw in (URL.format(key=key.lower()) + "?focusedId=1", URL.format(key=key), key):
        obj = cls(actor_email="a@example.test", **{field: raw}, **kwargs)
        assert getattr(obj, field) == key


def test_design_url_with_a_non_pq_key_is_still_rejected():
    from app.schemas.test_lifecycle import TlFeatureUpdate

    with pytest.raises(ValueError, match="Chiave Jira non valida per la progettazione"):
        TlFeatureUpdate(actor_email="a@example.test", jira_design_key=URL.format(key="ABC-9"))


def test_garbage_in_key_fields_is_still_rejected():
    from app.schemas.test_lifecycle import TlFeatureUpdate

    with pytest.raises(ValueError):
        TlFeatureUpdate(actor_email="a@example.test", jira_impl_key="not a key")


async def test_git_origin_revision_keeps_the_feature_keys(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    await _git_edit(fake, done, "Quando paga", "Quando paga con il bonifico")
    revisions = await _revisions(done["id"])
    assert [r.origin for r in revisions] == ["hub", "git"]
    assert revisions[1].jira_design_key == revisions[0].jira_design_key == "PQ-12"
    row = await _feature_row(done["id"])
    assert row.jira_design_key == "PQ-12"


async def test_git_change_during_a_reopened_draft_does_not_share_the_drafts_design_key(
    client, scope, actor, reviewer, fake
):
    from tests.test_tl_rebase import _reopened

    done, detail = await _reopened(client, scope, actor, reviewer, fake)  # draft with PQ-20
    await _git_edit(fake, done, "Quando paga", "Quando paga con il bonifico")
    revisions = await _revisions(done["id"])
    by_origin = {r.origin: r for r in revisions if r.origin == "git"}
    assert by_origin["git"].jira_design_key == "PQ-12"  # inherited from the published revision
    detail = await _get(client, done["id"], actor["email"])
    submit = next(t for t in detail["transitions"] if t["action"] == "submit")
    assert REUSED not in submit["reasons"]


async def test_backfill_covers_the_current_published_revision_without_a_working_one():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parent.parent / "alembic" / "versions" / "0043_revision_jira_keys.py"
    spec = importlib.util.spec_from_file_location("mig_0043b", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with TestSession() as s:
        scope, feature, (r1, r2) = await _feature_with_revisions(
            s, numbers=(1, 2), jira_design_key="PQ-1", jira_impl_key="ABC-2"
        )
        feature.published_revision_id = r2.id  # Rev. 2 is the current one, no working revision
        await s.commit()
        try:
            await s.execute(text(module.BACKFILL_SQL))
            await s.commit()
            async with TestSession() as other:
                current = await other.get(TlFeatureRevision, r2.id)
                assert (current.jira_design_key, current.jira_impl_key) == ("PQ-1", "ABC-2")
        finally:
            await s.execute(delete(TlScope).where(TlScope.id == scope.id))
            await s.commit()


async def test_create_task_guard_knows_which_revision_is_current(client, scope, actor, reviewer, jira):
    """The key built for an older revision must not land on a draft reopened during the Jira call."""
    from app.services.test_lifecycle import jira_tasks

    await _jira_scope(client, scope, jira)
    done = await _published(client, scope, actor, reviewer)
    await _reopen(client, done, actor)
    rev1, rev2 = await _revisions(done["id"])
    async with TestSession() as s:
        feature = await s.get(TlFeature, uuid.UUID(done["id"]))
        assert jira_tasks.is_current_revision(feature, rev2.id) is True
        assert jira_tasks.is_current_revision(feature, rev1.id) is False

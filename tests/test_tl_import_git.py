# ruff: noqa: S101, F811
"""Importa da Git, Task 2: importazione del file nella bozza."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlRevisionStatus,
    TlScenario,
    TlScenarioAlias,
    TlScenarioContent,
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

REPO_NAME = "pagopa/pagopa-platform-integration-test"
PATH = "src/integration/fdr3/feature/FEAT_011_FdR3_Recupero.feature"
BRANCH = "PQ-827-fdr-e2e-test-design"
URL = f"https://github.com/{REPO_NAME}/blob/{BRANCH}/{PATH}"
FILE = """#language: it
@fdr3_011_Recupero_FdR_Pubblicati_Organizzazione
Funzionalità: Recupero FdR pubblicati per organizzazione
  Descrizione del recupero.

  Contesto:
    Dato che i sistemi sono operativi

  @runnable
  @fdr3_011_01
  Scenario: Recupero di tutti i FdR pubblicati
    Quando l'organizzazione invia la richiesta
    Allora riceve 200

  @runnable @wip @negativo
  @fdr3_011_02
  Schema dello scenario: Recupero filtrato
    Dato il filtro <filtro>
    Quando l'organizzazione invia la richiesta
    Allora riceve <codice>

    Esempi:
      | filtro | codice |
      | psp    | 200    |
      | data   | 200    |
"""


@pytest.fixture
def git(monkeypatch):
    monkeypatch.setattr(settings, "tl_repo", REPO_NAME)
    fake = install_fake_git(monkeypatch, FakeGitClient())
    fake.branches[BRANCH] = fake.branches["main"]
    fake.seed_file(PATH, FILE, branch=BRANCH)
    return fake


async def _import(client, feat, actor, lock=0, url=URL):
    rev = feat["working_revision"]["id"]
    return await client.post(
        f"{BASE}/features/{feat['id']}/revisions/{rev}/import-git",
        json={"actor_email": actor["email"], "lock_version": lock, "url": url},
    )


async def _contents(rev_id):
    async with TestSession() as s:
        return (
            await s.execute(
                select(TlScenario, TlScenarioContent)
                .join(TlScenarioContent, TlScenarioContent.scenario_id == TlScenario.id)
                .where(TlScenarioContent.revision_id == uuid.UUID(rev_id))
                .order_by(TlScenarioContent.position)
            )
        ).all()


async def test_import_fills_the_draft(client, scope, actor, git):
    feat = await _new_feature(client, scope, actor, title="Recupero FdR")
    resp = await _import(client, feat, actor)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["replaced"] == 0 and body["ignored_tags"] == []  # custom tags are kept now
    detail = body["feature"]
    rev = detail["working_revision"]
    assert rev["title"] == "Recupero FdR" and rev["lock_version"] == 1
    assert rev["description"] == "Descrizione del recupero."
    assert rev["background_steps"] == "Dato che i sistemi sono operativi"
    src = detail["import_source"]
    assert (src["branch"], src["path"], src["url"]) == (BRANCH, PATH, URL)
    assert "cleanup" not in src  # the cleanup PR is gone: the source branch is never touched
    assert detail["git_path"] is None
    rows = await _contents(rev["id"])
    code = scope["code"].upper()
    assert [s.tag for s, _ in rows] == [f"@{code}_001_01", f"@{code}_001_02"]
    first, second = rows[0][1], rows[1][1]
    assert first.title == "Recupero di tutti i FdR pubblicati" and first.extra_tags == ["runnable"]
    assert second.kind.value == "outline" and second.polarity.value == "negativo"
    assert second.extra_tags == ["runnable", "wip"]
    assert second.examples["headers"] == ["filtro", "codice"] and len(second.examples["rows"]) == 2
    async with TestSession() as s:
        aliases = set(
            (
                await s.execute(
                    select(TlScenarioAlias.alias).where(
                        TlScenarioAlias.scenario_id.in_([sc.id for sc, _ in rows])
                    )
                )
            ).scalars()
        )
        events = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.resource_id == feat["id"], AuditLog.action == "tl.feature.imported")
            )
        ).scalar_one()
    assert aliases == {"fdr3_011_01", "fdr3_011_02"} and events == 1


async def test_import_replaces_existing_scenarios_and_is_repeatable(client, scope, actor, git):
    feat = await _new_feature(client, scope, actor)
    await _new_scenario(client, feat, actor, title="Vecchio 1")
    await _new_scenario(client, feat, actor, title="Vecchio 2")
    resp = await _import(client, feat, actor, lock=2)
    assert resp.status_code == 200, resp.text
    assert resp.json()["replaced"] == 2
    again = await _import(client, feat, actor, lock=3)
    assert again.status_code == 200, again.text
    rows = await _contents(feat["working_revision"]["id"])
    assert [s.number for s, _ in rows] == [1, 2]
    async with TestSession() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(TlScenario)
                .where(TlScenario.feature_id == uuid.UUID(feat["id"]))
            )
        ).scalar_one()
    assert n == 2


async def test_reopened_feature_keeps_published_identities(client, scope, actor, git):
    feat = await _new_feature(client, scope, actor)
    await _new_scenario(client, feat, actor, title="Pubblicato")
    fid, r1 = uuid.UUID(feat["id"]), uuid.UUID(feat["working_revision"]["id"])
    async with TestSession() as s:
        f = await s.get(TlFeature, fid)
        old = await s.get(TlFeatureRevision, r1)
        old.status = TlRevisionStatus.MERGED
        r2 = TlFeatureRevision(
            feature_id=fid,
            number=2,
            status=TlRevisionStatus.DRAFT,
            title=old.title,
            created_by=actor["email"],
            base_revision_id=r1,
        )
        s.add(r2)
        await s.flush()
        content = (
            await s.execute(select(TlScenarioContent).where(TlScenarioContent.revision_id == r1))
        ).scalar_one()
        s.add(
            TlScenarioContent(
                revision_id=r2.id,
                scenario_id=content.scenario_id,
                position=1,
                title=content.title,
                kind=content.kind,
                polarity=content.polarity,
                extra_tags=[],
                steps=content.steps,
            )
        )
        f.published_revision_id, f.working_revision_id = r1, r2.id
        await s.commit()
        r2_id = str(r2.id)
    resp = await _import(client, {"id": feat["id"], "working_revision": {"id": r2_id}}, actor)
    assert resp.status_code == 200, resp.text
    rows = await _contents(r2_id)
    assert [s.number for s, _ in rows] == [2, 3]
    assert len(await _contents(str(r1))) == 1


async def test_bad_file_and_locked_revision(client, scope, actor, git):
    feat = await _new_feature(client, scope, actor)
    git.seed_file(
        "bad.feature", "Funzionalità: x\n  Scenario: senza step\n    Pippo\n", branch=BRANCH
    )
    bad = await _import(client, feat, actor, url=URL.replace(PATH, "bad.feature"))
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "import_parse_error"
    missing = await _import(client, feat, actor, url=URL.replace(PATH, "nope.feature"))
    assert missing.status_code == 404
    wrong = await _import(client, feat, actor, url="https://github.com/x/y/blob/main/a.feature")
    assert wrong.status_code == 422 and wrong.json()["detail"]["code"] == "import_url_invalid"
    assert await _contents(feat["working_revision"]["id"]) == []
    async with TestSession() as s:
        rev = await s.get(TlFeatureRevision, uuid.UUID(feat["working_revision"]["id"]))
        rev.status = TlRevisionStatus.IN_REVIEW
        await s.commit()
    locked = await _import(client, feat, actor)
    assert locked.status_code == 409



FEATURE_TAG = "@fdr3_011_Recupero_FdR_Pubblicati_Organizzazione"


async def test_import_sets_the_test_type_and_keeps_it_out_of_the_scenarios(
    client, scope, actor, git
):
    git.seed_file(PATH, FILE.replace(f"{FEATURE_TAG}\n", f"{FEATURE_TAG} @api\n", 1), branch=BRANCH)
    feat = await _new_feature(client, scope, actor, title="Recupero FdR")
    resp = await _import(client, feat, actor)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["feature"]["test_type"] == "api"
    rows = await _contents(body["feature"]["working_revision"]["id"])
    assert all("api" not in c.extra_tags for _, c in rows)


async def test_import_without_test_type_keeps_the_one_of_the_feature(client, scope, actor, git):
    feat = await _new_feature(client, scope, actor, title="Recupero FdR", test_type="e2e")
    resp = await _import(client, feat, actor)
    assert resp.status_code == 200, resp.text
    assert resp.json()["feature"]["test_type"] == "e2e"


async def test_import_reports_tags_that_cannot_be_kept(client, scope, actor, git):
    text = FILE.replace("  @runnable\n  @fdr3_011_01", "  @runnable @bad.tag\n  @fdr3_011_01", 1)
    git.seed_file(PATH, text, branch=BRANCH)
    feat = await _new_feature(client, scope, actor, title="Recupero FdR")
    body = (await _import(client, feat, actor)).json()
    assert "bad.tag" in body["ignored_tags"]
    rows = await _contents(body["feature"]["working_revision"]["id"])
    assert rows[0][1].extra_tags == ["runnable"]


async def test_feature_level_tags_are_pushed_by_the_catalog_not_by_what_is_in_use(
    client, scope, actor, git
):
    # `smoke` is in use on another feature: that must not turn it into a pushed tag
    other = await _new_feature(client, scope, actor, title="Altra")
    sc = (await client.post(
        f"{BASE}/features/{other['id']}/revisions/{other['working_revision']['id']}/scenarios",
        json={"actor_email": actor["email"], "title": "S"},
    )).json()
    resp = await client.patch(
        f"{BASE}/features/{other['id']}/revisions/{other['working_revision']['id']}"
        f"/scenarios/{sc['id']}",
        json={"actor_email": actor["email"], "lock_version": 1, "extra_tags": ["runnable", "smoke"]},
    )
    assert resp.status_code == 200, resp.text

    text = FILE.replace(f"{FEATURE_TAG}\n", f"{FEATURE_TAG} @smoke @flaky\n", 1)
    git.seed_file(PATH, text, branch=BRANCH)
    feat = await _new_feature(client, scope, actor, title="Recupero FdR")
    body = (await _import(client, feat, actor)).json()
    assert "smoke" in body["ignored_tags"]
    rows = await _contents(body["feature"]["working_revision"]["id"])
    assert all("smoke" not in c.extra_tags for _, c in rows)
    assert all("flaky" in c.extra_tags for _, c in rows)

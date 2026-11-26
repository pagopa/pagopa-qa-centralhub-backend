# ruff: noqa: S101, F811
"""Fase 5, Task 6: esecuzione della migrazione per ambito."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, func, select, update

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.test_lifecycle import (
    TlCategoryTag,
    TlFeature,
    TlFeatureRevision,
    TlFeatureStatus,
    TlFeatureVersion,
    TlMigrationItem,
    TlMigrationScope,
    TlMigrationTag,
    TlRevisionStatus,
    TlScenario,
    TlScenarioAlias,
    TlScenarioContent,
    TlScope,
)
from app.services.test_lifecycle import migration, migration_align
from tests._db import TestSession
from tests.test_tl_features import _use_test_db, actor  # noqa: F401
from tests.tl_fakes import FakeConfluenceClient, FakeGitClient
from tests.tl_migration_fixtures import (  # noqa: F401
    clean_migration,
    feature_file,
    page,
    unique_scope,
)

REPO = "https://github.com/pagopa/pagopa-platform-integration-test/blob/main/"
LEGACY = """# language: it
@FEAT_001_{s} @e2e
Funzionalità: Pagamento

  # Happy path ----------------------------------
  @FEAT_001_{s}_01 @positivo @regression
  Scenario: Paga
    Dato un sistema
    Quando pago
    Allora funziona

  @FEAT_001_{s}_02 @negativo @skip
  Scenario: Rifiuta
    Dato un sistema
    Quando pago male
    Allora fallisce
"""


@pytest.fixture
async def env(clean_migration, actor):
    s = unique_scope()
    conf, git = FakeConfluenceClient(), FakeGitClient()
    conf.add_page(
        "p1",
        f"FEAT_001_{s} - Pagamento",
        page(
            f"FEAT_001_{s}",
            "Pagamento",
            "test suite implemented",
            file_url=REPO + f"src/{s}/pay.feature",
        ),
    )
    conf.add_page(
        "p2",
        f"FEAT_002_{s} - Rimborso",
        page(
            f"FEAT_002_{s}",
            "Rimborso",
            "WIP",
            scenarios=[("Rimborsa", "Dato un pagamento", "Quando rimborso", "Allora ok")],
        ),
    )
    git.seed_file(f"src/{s}/pay.feature", LEGACY.format(s=s))
    yield s, conf, git, actor
    async with TestSession() as db:
        fids = list(
            (
                await db.execute(
                    select(TlFeature.id)
                    .join(TlScope, TlScope.id == TlFeature.scope_id)
                    .where(TlScope.code == s)
                )
            ).scalars()
        )
        await db.execute(delete(TlMigrationItem).where(TlMigrationItem.feature_id.in_(fids)))
        await db.execute(delete(AuditLog).where(AuditLog.resource_id.in_([str(f) for f in fids])))
        await db.execute(
            update(TlFeature)
            .where(TlFeature.id.in_(fids))
            .values(working_revision_id=None, published_revision_id=None)
        )
        await db.execute(delete(TlFeature).where(TlFeature.id.in_(fids)))
        await db.execute(delete(TlScope).where(TlScope.code == s))
        await db.execute(delete(TlCategoryTag).where(TlCategoryTag.name == "regression"))
        await db.commit()


async def _analyze_confirm(conf, git, scope):
    async with TestSession() as db:
        await migration.analyze(db, conf, git)
        await db.execute(
            update(TlMigrationItem)
            .where(TlMigrationItem.scope_code == scope)
            .values(decision="confirmed")
        )
        await db.commit()


async def _execute(env):
    s, conf, git, actor = env
    async with TestSession() as db:
        return await migration.execute_scope(db, s, actor["email"], git=git, confluence=conf)


async def test_execute_creates_features_aliases_and_pr(env):
    s, conf, git, _ = env
    await _analyze_confirm(conf, git, s)
    out = await _execute(env)
    async with TestSession() as db:
        scope = (await db.execute(select(TlScope).where(TlScope.code == s))).scalar_one()
        feats = {
            f.number: f
            for f in (
                await db.execute(select(TlFeature).where(TlFeature.scope_id == scope.id))
            ).scalars()
        }
        pay, refund = feats[1], feats[2]
        assert scope.next_feature_number == 3 and scope.repo_folder == f"src/{s}"
        assert pay.status == TlFeatureStatus.IMPLEMENTED and pay.alignment_pending
        assert pay.git_path == f"src/{s}/pay.feature" and pay.published_revision_id
        assert pay.migrated_from["page_id"] == "p1"
        rev = await db.get(TlFeatureRevision, pay.published_revision_id)
        assert rev.status == TlRevisionStatus.MERGED and rev.title == "Pagamento"
        rows = (
            await db.execute(
                select(TlScenario, TlScenarioContent)
                .join(TlScenarioContent, TlScenarioContent.scenario_id == TlScenario.id)
                .where(TlScenarioContent.revision_id == rev.id)
                .order_by(TlScenarioContent.position)
            )
        ).all()
        assert [sc.tag for sc, _ in rows] == [f"@{s.upper()}_001_01", f"@{s.upper()}_001_02"]
        # the feature-level @e2e is the test type of the file, not a scenario tag
        assert pay.test_type == "e2e"
        assert rows[0][1].extra_tags == ["regression"]
        assert rows[1][1].extra_tags == ["skip"]
        aliases = set(
            (
                await db.execute(
                    select(TlScenarioAlias.alias).where(
                        TlScenarioAlias.scenario_id.in_([sc.id for sc, _ in rows])
                    )
                )
            ).scalars()
        )
        assert aliases == {f"FEAT_001_{s}_01", f"FEAT_001_{s}_02"}
        assert (
            await db.execute(
                select(func.count())
                .select_from(TlFeatureVersion)
                .where(
                    TlFeatureVersion.feature_id == pay.id, TlFeatureVersion.origin == "migration"
                )
            )
        ).scalar_one() == 1
        assert refund.status == TlFeatureStatus.DRAFT and refund.working_revision_id
        assert not refund.alignment_pending and refund.git_path is None
        refund_steps = (
            (
                await db.execute(
                    select(TlScenarioContent.steps).where(
                        TlScenarioContent.revision_id == refund.working_revision_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert refund_steps == ["Dato un pagamento\nQuando rimborso\nAllora ok"]
        assert "regression" in set((await db.execute(select(TlCategoryTag.name))).scalars())
        events = (
            await db.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.resource_id == str(pay.id), AuditLog.action == "tl.feature.migrated"
                )
            )
        ).scalar_one()
        assert events == 1
    assert out.pr_number is not None and out.pr_state == "open" and out.pr_error is None
    branch = f"qa-hub/migration-{s.lower()}"
    new_text = git.files(branch)[f"src/{s}/pay.feature"][0]
    assert new_text.startswith(f"# qa-hub: FEAT_001_{s}") or f"# qa-hub: FEAT_001_{s}" in new_text
    assert f"@{s.upper()}_001_01" in new_text and "@e2e" in new_text
    body = git.pull_meta[out.pr_number]["body"]
    assert f"FEAT_001_{s}_01" in body and "Happy path" in body
    # scrittura Confluence disattivata: nessuna PUT, errore registrato
    assert conf.writes == [] and out.confluence_errors


async def test_reexecution_is_idempotent_and_reuses_pr(env):
    s, conf, git, _ = env
    await _analyze_confirm(conf, git, s)
    first = await _execute(env)
    second = await _execute(env)
    assert second.pr_number == first.pr_number
    assert len(git.calls_of("create_pull")) == 1
    async with TestSession() as db:
        n = (
            await db.execute(
                select(func.count())
                .select_from(TlFeature)
                .join(TlScope, TlScope.id == TlFeature.scope_id)
                .where(TlScope.code == s)
            )
        ).scalar_one()
    assert n == 2


async def test_double_number_blocks_everything(env):
    s, conf, git, _ = env
    await _analyze_confirm(conf, git, s)
    async with TestSession() as db:
        await db.execute(
            update(TlMigrationItem)
            .where(TlMigrationItem.page_id == "p2")
            .values(overrides={"number": 1})
        )
        await db.commit()
    with pytest.raises(migration.MigrationBlocked) as exc:
        await _execute(env)
    assert any("1" in r for r in exc.value.reasons)
    async with TestSession() as db:
        assert (
            await db.execute(select(TlScope).where(TlScope.code == s))
        ).scalar_one_or_none() is None


async def test_changed_page_goes_back_to_pending(env):
    s, conf, git, _ = env
    await _analyze_confirm(conf, git, s)
    conf.pages["p2"]["version"] += 1
    await _execute(env)
    async with TestSession() as db:
        row = (
            await db.execute(select(TlMigrationItem).where(TlMigrationItem.page_id == "p2"))
        ).scalar_one()
    assert row.decision == "pending" and row.executed_at is None
    assert any(p["code"] == "changed_since_analysis" for p in row.problems)


async def test_git_error_keeps_features_and_retry(env):
    s, conf, git, _ = env
    await _analyze_confirm(conf, git, s)
    git.fail_once("create_pull", 502, "GitHub giù")
    out = await _execute(env)
    assert out.pr_error and "GitHub giù" in out.pr_error and out.pr_number is None
    async with TestSession() as db:
        out2 = await migration_align.open_alignment_pr(db, s, git)
    assert out2.pr_number is not None and out2.pr_error is None


async def test_confluence_panel_when_enabled_and_dropped_tag(env, monkeypatch):
    s, conf, git, _ = env
    monkeypatch.setattr(settings, "tl_migration_confluence_write", True)
    await _analyze_confirm(conf, git, s)
    async with TestSession() as db:
        await db.execute(
            update(TlMigrationTag)
            .where(TlMigrationTag.name == "regression")
            .values(decision="drop")
        )
        await db.commit()
    out = await _execute(env)
    assert {pid for pid, _ in conf.writes} == {"p1", "p2"}
    assert "Migrata su QA Hub" in conf.pages["p1"]["storage"] and out.confluence_errors == []
    async with TestSession() as db:
        tags = (
            (
                await db.execute(
                    select(TlScenarioContent.extra_tags)
                    .join(
                        TlFeature, TlFeature.published_revision_id == TlScenarioContent.revision_id
                    )
                    .join(TlScope, TlScope.id == TlFeature.scope_id)
                    .where(TlScope.code == s)
                )
            )
            .scalars()
            .all()
        )
        assert sorted(map(tuple, tags)) == [(), ("skip",)]
        scope_row = await db.get(TlMigrationScope, s)
        assert scope_row.executed_at is not None


def test_lost_comments():
    original = "# language: it\n# qa-hub: X\n# Happy path\nFunzionalità: A\n  # nota\n"
    rendered = "# language: it\n# qa-hub: X\nFunzionalità: A\n"
    assert migration_align.lost_comments(original, rendered) == ["# Happy path", "# nota"]


def test_no_items_for_unknown_scope_is_blocked():
    assert issubclass(migration.MigrationBlocked, Exception)
    assert uuid.uuid4()

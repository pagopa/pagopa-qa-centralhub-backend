# ruff: noqa: S101, F811
"""Fase 5, Task 5: analisi della migrazione (nessuna scrittura su Confluence o Git)."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select

from app.models.test_lifecycle import TlFeatureStatus, TlMigrationItem, TlMigrationTag
from app.models.user import User
from app.services.test_lifecycle import migration
from tests._db import TestSession
from tests.test_tl_features import _use_test_db  # noqa: F401
from tests.tl_fakes import FakeConfluenceClient, FakeGitClient
from tests.tl_migration_fixtures import (  # noqa: F401
    clean_migration,
    feature_file,
    page,
    unique_scope,
)

REPO = "https://github.com/pagopa/pagopa-platform-integration-test/blob/main/"


async def _items():
    async with TestSession() as s:
        rows = (await s.execute(select(TlMigrationItem))).scalars().all()
        return {(r.page_id or r.file_path): r for r in rows}


async def _analyze(conf, git):
    async with TestSession() as db:
        return await migration.analyze(db, conf, git)


def _codes(item):
    return {p["code"] for p in item.problems}


async def test_matching_by_link_code_and_title(clean_migration):
    s = unique_scope()
    conf, git = FakeConfluenceClient(), FakeGitClient()
    conf.add_page(
        "p1", f"FEAT_001_{s} - Uno", page(f"FEAT_001_{s}", "Uno", file_url=REPO + "src/a/x.feature")
    )
    conf.add_page(
        "p2", f"FEAT_002_{s} - Due", page(f"FEAT_002_{s}", "Due", "test suite implemented")
    )
    conf.add_page(
        "p3", f"FEAT_003_{s} - Pagamento con carta", page(f"FEAT_003_{s}", "Pagamento con carta")
    )
    git.seed_file("src/a/x.feature", feature_file(None, "Qualcosa", [("@t1", "A")]))
    git.seed_file("src/b/due.feature", feature_file(f"@FEAT_002_{s}", "Due", [("@t2", "B")]))
    git.seed_file(
        "src/c/pay.feature", feature_file(None, "Pagamento con carta di credito", [("@t3", "C")])
    )
    git.seed_file("src/d/other.feature", feature_file(None, "Altro", [("@t4", "D")]))
    summary = await _analyze(conf, git)
    items = await _items()
    assert items["p1"].file_path == "src/a/x.feature"
    assert items["p2"].file_path == "src/b/due.feature"
    assert items["p2"].status == TlFeatureStatus.IMPLEMENTED
    assert items["p3"].file_path == "src/c/pay.feature"
    assert (items["p1"].scope_code, items["p1"].number, items["p1"].title) == (s, 1, "Uno")
    other = items["src/d/other.feature"]
    assert other.page_id is None and other.status == TlFeatureStatus.IMPLEMENTED
    assert "no_code" in _codes(other) and "file_without_page" in _codes(other)
    assert items["p1"].proposal["repo_folder"] == "src/a"
    assert summary.status == "idle" and summary.analyzed_at is not None
    assert git.put_messages == [] and conf.writes == []


async def test_double_number_and_page_without_file(clean_migration):
    s = unique_scope()
    conf, git = FakeConfluenceClient(), FakeGitClient()
    conf.add_page("p5", f"FEAT_005_{s} - Ordinamento", page(f"FEAT_005_{s}", "Ordinamento"))
    conf.add_page("p6", f"FEAT_005_{s} - Back", page(f"FEAT_005_{s}", "Back"))
    await _analyze(conf, git)
    items = await _items()
    assert "number_conflict" in _codes(items["p5"]) and "number_conflict" in _codes(items["p6"])
    assert "page_without_file" in _codes(items["p5"])


async def test_people_scenarios_and_problems(clean_migration):
    s = unique_scope()
    email = f"mig-{uuid.uuid4().hex[:8]}@example.test"
    async with TestSession() as db:
        user = User(email=email, name="Mario Rossi", role="qa_analyst", is_active=True)
        db.add(user)
        await db.commit()
        uid = user.id
    try:
        conf, git = FakeConfluenceClient(), FakeGitClient()
        conf.add_user("acc-1", "Mario Rossi", email)
        conf.add_user("acc-x", "Sconosciuto", None)
        conf.add_page(
            "p1",
            f"FEAT_001_{s} - Uno",
            page(
                f"FEAT_001_{s}",
                "Uno",
                "Approvato",
                owner="acc-1",
                team=True,
                jira="PQ-12",
                scenarios=[("Primo", "Dato un sistema", "Quando agisco", "Allora funziona")],
            ),
        )
        conf.add_page(
            "p2",
            f"FEAT_002_{s} - Due",
            page(f"FEAT_002_{s}", "Due", owner="acc-x", reviewer="acc-1"),
        )
        await _analyze(conf, git)
        items = await _items()
        one, two = items["p1"], items["p2"]
        assert one.status == TlFeatureStatus.DESIGNED
        assert one.proposal["owner_id"] == str(uid) and one.proposal["reviewer_id"] is None
        assert "reviewer_is_team" in _codes(one)
        assert one.proposal["jira_design_key"] == "PQ-12"
        assert one.proposal["scenarios"] == [
            {"title": "Primo", "steps": "Dato un sistema\nQuando agisco\nAllora funziona"}
        ]
        assert "owner_not_found" in _codes(two) and two.proposal["reviewer_id"] == str(uid)
    finally:
        async with TestSession() as db:
            await db.execute(delete(User).where(User.id == uid))
            await db.commit()


async def test_reanalysis_keeps_decisions_and_drops_vanished_rows(clean_migration):
    s = unique_scope()
    conf, git = FakeConfluenceClient(), FakeGitClient()
    conf.add_page("p1", f"FEAT_001_{s} - Uno", page(f"FEAT_001_{s}", "Uno"))
    conf.add_page("p2", f"FEAT_002_{s} - Due", page(f"FEAT_002_{s}", "Due"))
    await _analyze(conf, git)
    async with TestSession() as db:
        row = (
            await db.execute(select(TlMigrationItem).where(TlMigrationItem.page_id == "p1"))
        ).scalar_one()
        row.decision, row.overrides = "confirmed", {"number": 9}
        await db.commit()
    del conf.pages["p2"]
    await _analyze(conf, git)
    items = await _items()
    assert set(items) == {"p1"}
    assert items["p1"].decision == "confirmed" and items["p1"].overrides == {"number": 9}


async def test_tags_counted_and_duplicates(clean_migration):
    s = unique_scope()
    conf, git = FakeConfluenceClient(), FakeGitClient()
    git.seed_file(
        "src/a.feature",
        feature_file(
            f"@FEAT_001_{s}", "A", [(f"@FEAT_001_{s}_01 @regression @runnable @positivo", "Uno")]
        ),
    )
    git.seed_file(
        "src/b.feature",
        feature_file(f"@FEAT_002_{s}", "B", [(f"@FEAT_001_{s}_01 @regression", "Due")]),
    )
    git.seed_file("src/tas_pass/c.feature", feature_file(None, "Fixture", [("@x", "X")]))
    await _analyze(conf, git)
    async with TestSession() as db:
        tags = {t.name: t for t in (await db.execute(select(TlMigrationTag))).scalars()}
    assert tags["regression"].occurrences == 2 and tags["regression"].decision == "keep"
    assert "runnable" in tags and "positivo" not in tags
    assert not any(n.startswith("FEAT_") for n in tags)
    items = await _items()
    assert "duplicate_identity_tag" in _codes(items["src/a.feature"])
    assert "fixture_or_copy" in _codes(items["src/tas_pass/c.feature"])

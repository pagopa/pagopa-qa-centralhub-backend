# ruff: noqa: S101
"""Fase 5, Task 1: modello dati della migrazione."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.core.permissions import ACTION_CATALOG
from app.models.test_lifecycle import (
    TlCategoryTag,
    TlFeature,
    TlFeatureStatus,
    TlMigrationItem,
    TlMigrationScope,
    TlMigrationState,
    TlMigrationTag,
)
from tests._db import TestSession


async def test_items_unique_page_and_file():
    page = f"p{uuid.uuid4().hex[:10]}"
    async with TestSession() as s:
        s.add(TlMigrationItem(
            page_id=page, scope_code="X", title="T", status=TlFeatureStatus.DRAFT
        ))
        await s.commit()
    try:
        async with TestSession() as s:
            s.add(TlMigrationItem(page_id=page, scope_code="X", title="T2",
                                  status=TlFeatureStatus.DRAFT))
            with pytest.raises(IntegrityError):
                await s.commit()
        async with TestSession() as s:
            stmt = select(TlMigrationItem).where(TlMigrationItem.page_id == page)
            row = (await s.execute(stmt)).scalar_one()
            assert row.decision == "pending" and row.problems == [] and row.overrides == {}
    finally:
        async with TestSession() as s:
            await s.execute(delete(TlMigrationItem).where(TlMigrationItem.page_id == page))
            await s.commit()


async def test_category_tags_seeded_and_other_tables():
    async with TestSession() as s:
        names = set((await s.execute(select(TlCategoryTag.name))).scalars())
        assert {"runnable", "e2e", "ui", "skip"} <= names
        await s.get(TlMigrationState, 1)
        assert TlMigrationScope.__table__.c.pr_error is not None
        assert TlMigrationTag.__table__.c.decision is not None
        assert TlFeature.__table__.c.alignment_pending is not None


def test_permission_and_settings():
    entry = next(e for e in ACTION_CATALOG if e["key"] == "manage:tl_migration")
    assert entry["defaults"] == {
        "qa_manager": True, "qa_analyst": False, "qa_engineer": False, "guest": False
    }
    assert settings.tl_confluence_root_page_id == "2815950851"
    assert settings.tl_migration_confluence_write is False

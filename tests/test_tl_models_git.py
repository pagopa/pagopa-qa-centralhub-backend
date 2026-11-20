from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureStatus,
    TlIssue,
    TlIssueKind,
    TlIssueStatus,
    TlRevisionStatus,
    TlScope,
    TlStepPhrase,
)
from tests._db import TestSession


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    prefix = f"T{uuid.uuid4().hex[:8]}"
    async with TestSession() as session:
        session.info["prefix"] = prefix
        yield session
        await session.rollback()
    async with TestSession() as session:
        await session.execute(delete(TlScope).where(TlScope.code.like(f"{prefix}%")))
        await session.execute(delete(TlStepPhrase).where(TlStepPhrase.text.like(f"{prefix}%")))
        await session.commit()


async def _feature(db: AsyncSession) -> TlFeature:
    scope = TlScope(code=f"{db.info['prefix']}a", name="Test scope")
    db.add(scope)
    await db.flush()
    feature = TlFeature(
        scope_id=scope.id,
        number=1,
        code=f"FEAT_001_{scope.code}",
        status=TlFeatureStatus.DRAFT,
        created_by="t@example.com",
    )
    db.add(feature)
    await db.flush()
    return feature


async def test_issue_defaults_open(db: AsyncSession) -> None:
    feature = await _feature(db)
    issue = TlIssue(
        feature_id=feature.id,
        kind=TlIssueKind.STRUCTURAL_CHANGE,
        detail={"why": "x"},
    )
    db.add(issue)
    await db.flush()
    await db.refresh(issue)
    assert issue.status == TlIssueStatus.OPEN
    assert issue.created_at is not None
    assert issue.updated_at is not None
    assert issue.detail == {"why": "x"}
    assert issue.revision_id is None


async def test_step_phrase_unique_keyword_text(db: AsyncSession) -> None:
    text = f"{db.info['prefix']} un utente"
    db.add(TlStepPhrase(keyword="given", text=text))
    await db.flush()
    db.add(TlStepPhrase(keyword="when", text=text))  # different keyword: ok
    await db.flush()
    db.add(TlStepPhrase(keyword="given", text=text))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_revision_git_defaults(db: AsyncSession) -> None:
    feature = await _feature(db)
    rev = TlFeatureRevision(
        feature_id=feature.id,
        number=1,
        status=TlRevisionStatus.DRAFT,
        title="Titolo",
        created_by="t@example.com",
    )
    db.add(rev)
    await db.flush()
    await db.refresh(rev)
    assert rev.origin == "hub"
    assert rev.base_stale is False
    assert rev.pr_number is None
    assert rev.pr_state is None
    assert feature.git_path is None
    phrase = TlStepPhrase(keyword="then", text=f"{db.info['prefix']} ok")
    db.add(phrase)
    await db.flush()
    await db.refresh(phrase)
    assert phrase.occurrences == 0
    assert phrase.implemented is False

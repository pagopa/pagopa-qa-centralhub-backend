from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.models.status_page import SpChangeSummary, SpTechStack
from tests._db import TestSession

PREFIX = "pagopa/t34-"


def _repo() -> str:
    return PREFIX + uuid.uuid4().hex[:8]


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Any]:
    async with TestSession() as session:
        yield session
    async with TestSession() as s:
        await s.execute(
            delete(SpChangeSummary).where(SpChangeSummary.repository.like(f"{PREFIX}%"))
        )
        await s.execute(delete(SpTechStack).where(SpTechStack.repository.like(f"{PREFIX}%")))
        await s.commit()


@pytest.mark.asyncio
async def test_summary_defaults(db: Any) -> None:
    repo = _repo()
    db.add(SpChangeSummary(repository=repo, from_version="1.0.0", to_version="1.1.0", status="OK"))
    await db.commit()
    async with TestSession() as s:
        row = await s.scalar(select(SpChangeSummary).where(SpChangeSummary.repository == repo))
        assert row is not None
        assert row.items == []
        assert row.total_commits == 0
        assert row.truncated is False
        assert row.attempts == 0
        assert row.direction is None
        assert row.error is None
        assert row.fetched_at is not None


@pytest.mark.asyncio
async def test_summary_duplicate_pair_rejected(db: Any) -> None:
    repo = _repo()
    db.add(SpChangeSummary(repository=repo, from_version="1", to_version="2", status="OK"))
    await db.commit()
    db.add(SpChangeSummary(repository=repo, from_version="1", to_version="2", status="ERROR"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_summary_invalid_status_rejected(db: Any) -> None:
    db.add(SpChangeSummary(repository=_repo(), from_version="1", to_version="2", status="X"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_summary_invalid_direction_rejected(db: Any) -> None:
    db.add(
        SpChangeSummary(
            repository=_repo(), from_version="1", to_version="2", status="OK", direction="x"
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_stack_round_trip(db: Any) -> None:
    repo = _repo()
    db.add(
        SpTechStack(
            repository=repo,
            status="OK",
            language="JAVA",
            version="17",
            source="dockerfile",
            for_version="1.2.3",
            ref="v1.2.3",
        )
    )
    await db.commit()
    async with TestSession() as s:
        row = await s.get(SpTechStack, repo)
        assert row is not None
        assert (row.language, row.version, row.source) == ("JAVA", "17", "dockerfile")
        assert row.for_version == "1.2.3"
        assert row.ref == "v1.2.3"
        assert row.attempts == 0
        assert row.detected_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kw", [{"status": "X"}, {"language": "RUBY"}, {"source": "magic"}], ids=str
)
async def test_stack_invalid_values_rejected(db: Any, kw: dict[str, str]) -> None:
    values: dict[str, Any] = {"repository": _repo(), "status": "OK", "for_version": "1"}
    values.update(kw)
    db.add(SpTechStack(**values))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


def test_settings_defaults() -> None:
    s = Settings()
    assert s.status_page_enrich_batch == 40
    assert s.status_page_github_reserve == 1000

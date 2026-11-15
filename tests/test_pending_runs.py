from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_metrics import RevisionResultEnum
from app.models.test_metrics import TestRun as RunModel
from app.models.test_metrics import TestSuite as SuiteModel
from app.schemas.test_metrics import PendingRunsOut
from app.services.test_metrics import get_pending_runs
from tests._db import TestSession


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def suite(db: AsyncSession) -> AsyncIterator[SuiteModel]:
    created = SuiteModel(
        test_object=f"pending-{uuid.uuid4().hex[:8]}", test_type="e2e", test_object_version="1"
    )
    db.add(created)
    await db.commit()
    suite_id = created.id
    yield created
    await db.rollback()
    await db.execute(delete(SuiteModel).where(SuiteModel.id == suite_id))
    await db.commit()


async def test_pending_runs_match_the_nrt_and_quality_gate_lists(
    db: AsyncSession, suite: SuiteModel
) -> None:
    """Only runs a reviewer can find in the lists: same origins and same 30-day window."""
    before = await get_pending_runs(db)
    recent = datetime.now(UTC) - timedelta(days=2)
    old = datetime.now(UTC) - timedelta(days=31)
    runs = [
        ("NRT", RevisionResultEnum.PENDING, recent),
        ("NRT", RevisionResultEnum.PENDING, recent),
        ("NRT", RevisionResultEnum.OK, recent),
        ("NRT", None, recent),
        ("NRT", RevisionResultEnum.PENDING, old),  # outside the list window
        ("NRT", RevisionResultEnum.PENDING, None),  # never finished: not listed
        ("QG", RevisionResultEnum.PENDING, recent),
        ("QG", RevisionResultEnum.PENDING, old),
        # Not sent by the ingestion; the NRT list shows it, so it counts as NRT.
        ("QA_GATE", RevisionResultEnum.PENDING, recent),
        ("TAS", RevisionResultEnum.PENDING, recent),
        (None, RevisionResultEnum.PENDING, recent),
    ]
    db.add_all(
        RunModel(suite_id=suite.id, test_origin=origin, revision_result=result, timestamp_end=end)
        for origin, result, end in runs
    )
    await db.commit()

    after = await get_pending_runs(db)

    assert after.nrt - before.nrt == 3
    assert after.qg - before.qg == 1
    assert after.total == after.nrt + after.qg


@pytest.mark.anyio
async def test_pending_runs_endpoint(client: AsyncClient) -> None:
    with patch(
        "app.services.test_metrics.get_pending_runs",
        new_callable=AsyncMock,
        return_value=PendingRunsOut(total=7, nrt=5, qg=2),
    ):
        response = await client.get("/api/v1/test-metrics/pending-runs")

    assert response.status_code == 200
    assert response.json() == {"total": 7, "nrt": 5, "qg": 2}

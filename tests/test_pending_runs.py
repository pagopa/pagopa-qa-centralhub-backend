from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator
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


async def test_pending_runs_count_nrt_and_quality_gate_only(
    db: AsyncSession, suite: SuiteModel
) -> None:
    before = await get_pending_runs(db)
    runs = [
        ("NRT", RevisionResultEnum.PENDING),
        ("NRT", RevisionResultEnum.PENDING),
        ("NRT", RevisionResultEnum.OK),
        ("QG", RevisionResultEnum.PENDING),
        ("QA_GATE", RevisionResultEnum.PENDING),
        ("TAS", RevisionResultEnum.PENDING),
        (None, RevisionResultEnum.PENDING),
        ("NRT", None),
    ]
    db.add_all(
        RunModel(suite_id=suite.id, test_origin=origin, revision_result=result)
        for origin, result in runs
    )
    await db.commit()

    after = await get_pending_runs(db)

    assert after.nrt - before.nrt == 2
    assert after.qg - before.qg == 2
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

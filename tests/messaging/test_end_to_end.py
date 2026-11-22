"""End to end: producer, rules, dispatcher, inbox, through the real HTTP route and scanners."""

from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.deps import get_db
from app.main import app
from app.messaging.catalog import NRT_PENDING_KEY
from app.messaging.producers import quality_gate as qg_producer
from app.messaging.scanners import nrt_pending
from app.messaging.transport import PostgresTransport
from app.models.messaging import MsgDedupe, MsgInbox
from app.models.test_metrics import TestSuite as SuiteModel
from tests._db import TestSession, get_test_db
from tests.messaging.helpers import users


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_db] = get_test_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


async def _inbox(user_ids: list[uuid.UUID]) -> list[MsgInbox]:
    async with TestSession() as s:
        return list(
            (await s.execute(select(MsgInbox).where(MsgInbox.user_id.in_(user_ids)))).scalars()
        )


async def test_a_failed_quality_gate_reaches_the_three_qa_roles_and_not_the_guest(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qg_producer, "wake_dispatcher", lambda: None)
    suite = SuiteModel(
        id=uuid.uuid4(), test_object="e2e-obj", test_type="integration", test_object_version="1"
    )
    async with TestSession() as s:
        s.add(suite)
        await s.commit()
    try:
        async with users(
            ("qa_manager", True), ("qa_analyst", True), ("qa_engineer", True), ("guest", True)
        ) as (manager, analyst, engineer, guest):
            response = await client.post(
                "/api/v1/test-metrics/test-runs",
                json={
                    "suite_id": str(suite.id),
                    "scenario_qty": 10,
                    "passed_scenario": 8,
                    "failed_scenario": 2,
                    "broken_scenario": 0,
                    "skipped_scenario": 0,
                    "env": "UAT",
                    "test_origin": "QG",
                },
            )
            assert response.status_code == 200, response.text
            run_id = response.json()["id"]

            await PostgresTransport(TestSession).drain_due(limit=100)

            rows = await _inbox([manager, analyst, engineer, guest])
            assert {r.user_id for r in rows} == {manager, analyst, engineer}
            for row in rows:
                assert row.read_at is None
                assert row.severity == "warning"
                assert row.link == f"/quality-gate/report/{run_id}"
                assert row.title == "Quality Gate non superato · e2e-obj / integration"
    finally:
        async with TestSession() as s:
            await s.execute(delete(SuiteModel).where(SuiteModel.id == suite.id))
            await s.commit()


async def test_repeated_scans_leave_exactly_one_nrt_message_per_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = [uuid.uuid4(), uuid.uuid4()]

    async def fake_ids(db: object) -> list[uuid.UUID]:
        return pending

    monkeypatch.setattr(nrt_pending, "list_pending_nrt_run_ids", fake_ids)
    monkeypatch.setattr(nrt_pending, "wake_dispatcher", lambda: None)
    try:
        async with users(("qa_manager", True), ("qa_engineer", True)) as (manager, engineer):
            for _ in range(3):
                async with TestSession() as s:
                    await nrt_pending.scan(s)
                await PostgresTransport(TestSession).drain_due(limit=100)

            rows = [
                r
                for r in await _inbox([manager, engineer])
                if r.type.endswith("nrt.reviews.pending")
            ]
            assert sorted(r.user_id for r in rows) == sorted([manager, engineer])
            assert all(r.title == "2 run NRT in attesa di analisi" for r in rows)
    finally:
        async with TestSession() as s:
            await s.execute(delete(MsgDedupe).where(MsgDedupe.key == NRT_PENDING_KEY))
            await s.commit()

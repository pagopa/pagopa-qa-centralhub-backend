from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.deps import get_db
from app.main import app
from app.messaging.catalog import QG_RUN_COMPLETED
from app.messaging.envelope import deterministic_id
from app.messaging.producers import quality_gate as producer
from app.messaging.producers.quality_gate import (
    build_quality_gate_event,
    emit_quality_gate_completed,
)
from app.models.messaging import MsgDelivery, MsgEvent
from app.models.test_metrics import EnvEnum, TestRun, TestSuite
from tests._db import TestSession, get_test_db


def _suite() -> TestSuite:
    return TestSuite(
        id=uuid.uuid4(), test_object="pagopa-fdr", test_type="integration", test_object_version="1"
    )


def _run(suite: TestSuite, **counts: int | None) -> TestRun:
    values: dict[str, int | None] = {
        "scenario_qty": 148,
        "passed_scenario": 136,
        "failed_scenario": 8,
        "broken_scenario": 3,
        "skipped_scenario": 1,
    }
    values.update(counts)
    return TestRun(
        id=uuid.uuid4(),
        suite_id=suite.id,
        env=EnvEnum.UAT,
        test_origin="QG",
        timestamp_end=datetime.now(UTC),
        **values,
    )


def test_build_event_for_a_failed_run() -> None:
    suite = _suite()
    run = _run(suite)
    event = build_quality_gate_event(run, suite)

    assert event.type == QG_RUN_COMPLETED
    assert event.source == "/qa-hub/quality-gate"
    assert event.subject == str(run.id)
    assert event.id == deterministic_id(QG_RUN_COMPLETED, str(run.id))
    assert event.severity == "warning"
    assert event.environment == "uat"
    assert event.data == {
        "run_id": str(run.id),
        "test_object": "pagopa-fdr",
        "test_type": "integration",
        "environment": "uat",
        "scenario_qty": 148,
        "passed": 136,
        "failed": 8,
        "broken": 3,
        "skipped": 1,
    }


def test_build_event_is_info_without_failures_and_treats_none_as_zero() -> None:
    suite = _suite()
    passed = build_quality_gate_event(
        _run(suite, failed_scenario=0, broken_scenario=0, skipped_scenario=0), suite
    )
    assert passed.severity == "info"

    sparse = build_quality_gate_event(
        _run(
            suite,
            scenario_qty=None,
            passed_scenario=None,
            failed_scenario=None,
            broken_scenario=None,
            skipped_scenario=None,
        ),
        suite,
    )
    assert sparse.severity == "info"
    assert sparse.data["scenario_qty"] == 0
    assert sparse.data["failed"] == 0

    only_skipped = build_quality_gate_event(
        _run(suite, failed_scenario=None, broken_scenario=None, skipped_scenario=2), suite
    )
    assert only_skipped.severity == "warning"


def test_build_event_is_deterministic_per_run() -> None:
    suite = _suite()
    run = _run(suite)
    assert build_quality_gate_event(run, suite).id == build_quality_gate_event(run, suite).id


@pytest.fixture
async def stored() -> AsyncIterator[tuple[TestSuite, TestRun]]:
    suite, run = _suite(), None
    async with TestSession() as s:
        s.add(suite)
        await s.flush()
        run = _run(suite)
        s.add(run)
        await s.commit()
    try:
        yield suite, run
    finally:
        async with TestSession() as s:
            await s.execute(
                delete(MsgEvent).where(
                    MsgEvent.id == uuid.UUID(deterministic_id(QG_RUN_COMPLETED, str(run.id)))
                )
            )
            await s.execute(delete(TestSuite).where(TestSuite.id == suite.id))
            await s.commit()


@pytest.fixture(autouse=True)
def _no_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(producer, "wake_dispatcher", lambda: None)


async def _events_for(run: TestRun) -> int:
    async with TestSession() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(MsgEvent)
                .where(MsgEvent.id == uuid.UUID(deterministic_id(QG_RUN_COMPLETED, str(run.id))))
            )
        ).scalar_one()


async def test_emit_creates_one_event_and_one_inbox_delivery(
    stored: tuple[TestSuite, TestRun],
) -> None:
    _, run = stored
    async with TestSession() as s:
        await emit_quality_gate_completed(s, run)
    assert await _events_for(run) == 1
    async with TestSession() as s:
        deliveries = (
            (
                await s.execute(
                    select(MsgDelivery).where(
                        MsgDelivery.event_id
                        == uuid.UUID(deterministic_id(QG_RUN_COMPLETED, str(run.id)))
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [d.consumer for d in deliveries] == ["inbox"]


async def test_emitting_twice_for_the_same_run_creates_one_event(
    stored: tuple[TestSuite, TestRun],
) -> None:
    _, run = stored
    async with TestSession() as s:
        await emit_quality_gate_completed(s, run)
    async with TestSession() as s:
        await emit_quality_gate_completed(s, run)
    assert await _events_for(run) == 1


async def test_a_publish_failure_is_swallowed(
    stored: tuple[TestSuite, TestRun], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run = stored

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("bus down")

    monkeypatch.setattr(producer, "publish", boom)
    async with TestSession() as s:
        await emit_quality_gate_completed(s, run)  # must not raise
    assert await _events_for(run) == 0
    async with TestSession() as s:
        assert await s.get(TestRun, run.id) is not None


async def _post_run(client: AsyncClient, suite: TestSuite, origin: str) -> dict[str, object]:
    response = await client.post(
        "/api/v1/test-metrics/test-runs",
        json={
            "suite_id": str(suite.id),
            "scenario_qty": 3,
            "passed_scenario": 2,
            "failed_scenario": 1,
            "broken_scenario": 0,
            "skipped_scenario": 0,
            "env": "UAT",
            "test_origin": origin,
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


@pytest.fixture
async def route_client() -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_db] = get_test_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


async def test_posting_a_qg_run_publishes_an_event_but_nrt_and_tas_do_not(
    route_client: AsyncClient,
) -> None:
    suite = _suite()
    async with TestSession() as s:
        s.add(suite)
        await s.commit()
    run_ids: list[str] = []
    try:
        for origin, expected in [("QG", 1), ("NRT", 0), ("TAS", 0)]:
            run_id = str((await _post_run(route_client, suite, origin))["id"])
            run_ids.append(run_id)
            async with TestSession() as s:
                count = (
                    await s.execute(
                        select(func.count())
                        .select_from(MsgEvent)
                        .where(MsgEvent.id == uuid.UUID(deterministic_id(QG_RUN_COMPLETED, run_id)))
                    )
                ).scalar_one()
            assert count == expected, origin
    finally:
        async with TestSession() as s:
            await s.execute(
                delete(MsgEvent).where(
                    MsgEvent.id.in_(
                        [uuid.UUID(deterministic_id(QG_RUN_COMPLETED, r)) for r in run_ids]
                    )
                )
            )
            await s.execute(delete(TestSuite).where(TestSuite.id == suite.id))
            await s.commit()


async def test_the_route_response_is_unchanged_when_the_emitter_fails(
    route_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("bus down")

    monkeypatch.setattr(producer, "publish", boom)
    suite = _suite()
    async with TestSession() as s:
        s.add(suite)
        await s.commit()
    try:
        body = await _post_run(route_client, suite, "QG")
        assert body["test_origin"] == "QG"
        assert body["scenario_qty"] == 3
    finally:
        async with TestSession() as s:
            await s.execute(delete(TestSuite).where(TestSuite.id == suite.id))
            await s.commit()


async def test_the_run_and_its_event_commit_together_or_not_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit failing after the run was stored must not leave a run without its event."""
    from sqlalchemy.ext.asyncio import AsyncSession

    original_commit = AsyncSession.commit
    calls = {"n": 0}

    async def second_commit_fails(self: AsyncSession) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database went away")
        await original_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", second_commit_fails)
    suite = _suite()
    async with TestSession() as s:
        s.add(suite)
        await s.commit()
    calls["n"] = 0
    app.dependency_overrides[get_db] = get_test_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/test-metrics/test-runs",
                json={
                    "suite_id": str(suite.id),
                    "scenario_qty": 3,
                    "passed_scenario": 2,
                    "failed_scenario": 1,
                    "broken_scenario": 0,
                    "skipped_scenario": 0,
                    "env": "UAT",
                    "test_origin": "QG",
                },
            )
        async with TestSession() as s:
            runs = (await s.execute(select(TestRun.id).where(TestRun.suite_id == suite.id))).all()
            events = (
                await s.execute(
                    select(MsgEvent.id).where(
                        MsgEvent.id.in_(
                            [uuid.UUID(deterministic_id(QG_RUN_COMPLETED, str(r.id))) for r in runs]
                            or [uuid.uuid4()]
                        )
                    )
                )
            ).all()
        assert len(runs) == len(events), f"{len(runs)} run(s) but {len(events)} event(s)"
        assert response.status_code in (200, 500)
    finally:
        app.dependency_overrides.pop(get_db, None)
        monkeypatch.setattr(AsyncSession, "commit", original_commit)
        async with TestSession() as s:
            await s.execute(delete(TestSuite).where(TestSuite.id == suite.id))
            await s.commit()


async def test_a_failing_publish_still_stores_the_run(
    route_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("bus down")

    monkeypatch.setattr(producer, "publish", boom)
    suite = _suite()
    async with TestSession() as s:
        s.add(suite)
        await s.commit()
    try:
        body = await _post_run(route_client, suite, "QG")
        async with TestSession() as s:
            assert await s.get(TestRun, uuid.UUID(str(body["id"]))) is not None
    finally:
        async with TestSession() as s:
            await s.execute(delete(TestSuite).where(TestSuite.id == suite.id))
            await s.commit()

from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select

from app.messaging.bus import PublishResult, PublishStatus
from app.messaging.catalog import NRT_PENDING_KEY, SANP_FAILING_KEY
from app.messaging.scanners import SCANNERS, Scanner, nrt_pending, run_all, sanp_failing
from app.models.messaging import MsgDedupe, MsgEvent
from app.models.sanp_health import (
    SanpEnvironment,
    SanpHealthResult,
    SanpHealthRun,
    SanpImportStatus,
    SanpResultStatus,
)
from app.models.test_metrics import RevisionResultEnum
from app.models.test_metrics import TestRun as RunModel
from app.models.test_metrics import TestSuite as SuiteModel
from app.services.test_metrics import get_pending_runs, list_pending_nrt_run_ids
from tests._db import TestSession


@pytest.fixture(autouse=True)
def _no_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nrt_pending, "wake_dispatcher", lambda: None)
    monkeypatch.setattr(sanp_failing, "wake_dispatcher", lambda: None)


async def _cleanup(event_ids: list[str], keys: list[str]) -> None:
    async with TestSession() as s:
        await s.execute(delete(MsgEvent).where(MsgEvent.id.in_([uuid.UUID(i) for i in event_ids])))
        await s.execute(delete(MsgDedupe).where(MsgDedupe.key.in_(keys)))
        await s.commit()


# ---------------------------------------------------------------- NRT pending reviews


async def test_pending_nrt_ids_follow_the_list_rules_and_match_get_pending_runs() -> None:
    suite = SuiteModel(
        id=uuid.uuid4(),
        test_object=f"scan-{uuid.uuid4().hex[:8]}",
        test_type="NRT",
        test_object_version="1",
    )
    recent = datetime.now(UTC) - timedelta(days=2)
    old = datetime.now(UTC) - timedelta(days=40)
    cases = {
        "nrt_pending": ("NRT-SCAN", RevisionResultEnum.PENDING, recent),
        "nrt_ok": ("NRT-SCAN", RevisionResultEnum.OK, recent),
        "nrt_old": ("NRT-SCAN", RevisionResultEnum.PENDING, old),
        "tas": ("TAS", RevisionResultEnum.PENDING, recent),
        "qg": ("QG", RevisionResultEnum.PENDING, recent),
    }
    runs = {
        name: RunModel(
            id=uuid.uuid4(),
            suite_id=suite.id,
            test_origin=origin,
            revision_result=result,
            timestamp_end=end,
        )
        for name, (origin, result, end) in cases.items()
    }
    async with TestSession() as s:
        s.add(suite)
        await s.flush()
        s.add_all(runs.values())
        await s.commit()
    try:
        async with TestSession() as s:
            ids = set(await list_pending_nrt_run_ids(s))
            assert runs["nrt_pending"].id in ids
            for excluded in ("nrt_ok", "nrt_old", "tas", "qg"):
                assert runs[excluded].id not in ids
            assert len(ids) == (await get_pending_runs(s)).nrt
    finally:
        async with TestSession() as s:
            await s.execute(delete(SuiteModel).where(SuiteModel.id == suite.id))
            await s.commit()


async def test_nrt_scan_reports_once_then_only_when_something_new_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending: list[uuid.UUID] = []

    async def fake_ids(db: Any) -> list[uuid.UUID]:
        return list(pending)

    monkeypatch.setattr(nrt_pending, "list_pending_nrt_run_ids", fake_ids)
    a, b, c = (uuid.uuid4() for _ in range(3))
    event_ids: list[str] = []

    async def scan() -> PublishResult | None:
        async with TestSession() as s:
            result = await nrt_pending.scan(s)
        if result is not None:
            event_ids.append(result.event_id)
        return result

    try:
        pending[:] = [b, a]
        first = await scan()
        assert first is not None and first.status is PublishStatus.ACCEPTED
        async with TestSession() as s:
            event = (
                await s.execute(select(MsgEvent).where(MsgEvent.id == uuid.UUID(first.event_id)))
            ).scalar_one()
        assert event.severity == "info"
        assert event.data == {"count": 2, "run_ids": sorted([str(a), str(b)])}

        again = await scan()
        assert again is not None and again.status is PublishStatus.SUPPRESSED

        pending[:] = [a, b, c]
        grown = await scan()
        assert grown is not None and grown.status is PublishStatus.ACCEPTED

        pending[:] = []
        assert await scan() is None  # condition cleared
        async with TestSession() as s:
            assert (await s.get(MsgDedupe, NRT_PENDING_KEY)) is None

        pending[:] = [a]
        reappeared = await scan()
        assert reappeared is not None and reappeared.status is PublishStatus.ACCEPTED
    finally:
        await _cleanup(event_ids, [NRT_PENDING_KEY])


# ---------------------------------------------------------------- SANP failing checks


def _run(completed: datetime, status: SanpImportStatus) -> SanpHealthRun:
    return SanpHealthRun(
        id=uuid.uuid4(),
        github_run_id=int(uuid.uuid4().int % 10**12),
        run_number=1,
        head_sha="a" * 40,
        workflow_branch="main",
        conclusion="success",
        html_url="https://example.org/run",
        started_at=completed,
        completed_at=completed,
        import_status=status,
    )


def _result(
    run: SanpHealthRun, spec: str, env: SanpEnvironment, status: SanpResultStatus
) -> SanpHealthResult:
    return SanpHealthResult(
        run_id=run.id,
        spec_name=spec,
        environment=env,
        source_branch="main",
        target_apim="apim",
        display_name=spec,
        description="d",
        status=status,
        raw_payload={},
    )


@pytest.fixture
async def sanp_runs() -> AsyncIterator[dict[str, SanpHealthRun]]:
    """Far-future runs, so that they are the latest whatever the local data holds."""
    older = _run(datetime(2098, 1, 1, tzinfo=UTC), SanpImportStatus.COMPLETE)
    latest = _run(datetime(2099, 1, 1, tzinfo=UTC), SanpImportStatus.COMPLETE)
    partial = _run(datetime(2100, 1, 1, tzinfo=UTC), SanpImportStatus.PARTIAL)
    async with TestSession() as s:
        s.add_all([older, latest, partial])
        await s.flush()
        s.add_all(
            [
                _result(older, "old-ko", SanpEnvironment.SANP, SanpResultStatus.KO),
                _result(partial, "partial-ko", SanpEnvironment.SANP, SanpResultStatus.KO),
                _result(latest, "a", SanpEnvironment.SANP, SanpResultStatus.KO),
                _result(latest, "b", SanpEnvironment.COLLAUDO, SanpResultStatus.WARNING),
                _result(latest, "c", SanpEnvironment.SANP, SanpResultStatus.INFO),
                _result(latest, "d", SanpEnvironment.SANP, SanpResultStatus.OK),
                _result(latest, "e", SanpEnvironment.SANP, SanpResultStatus.NOT_CONFIGURED),
            ]
        )
        await s.commit()
    try:
        yield {"older": older, "latest": latest, "partial": partial}
    finally:
        async with TestSession() as s:
            await s.execute(
                delete(SanpHealthRun).where(SanpHealthRun.id.in_([older.id, latest.id, partial.id]))
            )
            await s.commit()


async def test_sanp_scan_counts_ko_and_warning_from_the_latest_complete_import(
    sanp_runs: dict[str, SanpHealthRun],
) -> None:
    event_ids: list[str] = []
    try:
        async with TestSession() as s:
            result = await sanp_failing.scan(s)
        assert result is not None and result.status is PublishStatus.ACCEPTED
        event_ids.append(result.event_id)
        async with TestSession() as s:
            event = (
                await s.execute(select(MsgEvent).where(MsgEvent.id == uuid.UUID(result.event_id)))
            ).scalar_one()
        assert event.severity == "warning"
        assert event.data == {"ko": 1, "warning": 1, "items": ["COLLAUDO:b", "SANP:a"]}

        async with TestSession() as s:
            again = await sanp_failing.scan(s)
        assert again is not None and again.status is PublishStatus.SUPPRESSED
    finally:
        await _cleanup(event_ids, [SANP_FAILING_KEY])


async def test_sanp_status_flip_does_not_renotify_and_all_ok_clears(
    sanp_runs: dict[str, SanpHealthRun],
) -> None:
    event_ids: list[str] = []
    try:
        async with TestSession() as s:
            first = await sanp_failing.scan(s)
        assert first is not None
        event_ids.append(first.event_id)

        async with TestSession() as s:
            row = (
                await s.execute(
                    select(SanpHealthResult).where(
                        SanpHealthResult.run_id == sanp_runs["latest"].id,
                        SanpHealthResult.spec_name == "b",
                    )
                )
            ).scalar_one()
            row.status = SanpResultStatus.KO  # WARNING -> KO: same member, no new message
            await s.commit()
        async with TestSession() as s:
            flipped = await sanp_failing.scan(s)
        assert flipped is not None and flipped.status is PublishStatus.SUPPRESSED

        async with TestSession() as s:
            for result in (
                await s.execute(
                    select(SanpHealthResult).where(
                        SanpHealthResult.run_id == sanp_runs["latest"].id
                    )
                )
            ).scalars():
                result.status = SanpResultStatus.OK
            await s.commit()
        async with TestSession() as s:
            assert await sanp_failing.scan(s) is None
            assert (await s.get(MsgDedupe, SANP_FAILING_KEY)) is None
    finally:
        await _cleanup(event_ids, [SANP_FAILING_KEY])


# ---------------------------------------------------------------- run_all


async def test_run_all_isolates_a_failing_scanner() -> None:
    ran: list[str] = []

    async def bad(db: Any) -> None:
        raise RuntimeError("scanner exploded")

    async def good(db: Any) -> None:
        ran.append("good")

    results = await run_all(TestSession, scanners=(Scanner("bad", bad), Scanner("good", good)))
    assert results == {"bad": "error", "good": "ok"}
    assert ran == ["good"]


def test_phase_one_scanners_are_registered() -> None:
    assert [s.name for s in SCANNERS] == ["nrt_pending", "sanp_failing"]

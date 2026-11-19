from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text

from app.config import settings
from app.models.status_page import (
    SpApplication,
    SpChangeSummary,
    SpDeployEvent,
    SpProduct,
    SpTechStack,
    SpVersionState,
)
from app.services import status_page_enrich as svc
from app.services.status_page_changes import ChangeResult
from app.services.status_page_github import GitHubBudgetExhaustedError
from app.services.status_page_stack import StackResult
from tests._db import TestSession

T0 = datetime(2026, 4, 1, 10, tzinfo=UTC)


class Ctx:
    def __init__(self) -> None:
        sfx = uuid.uuid4().hex[:8]
        self.repo = f"zz-enr-{sfx}"
        self.product_id = uuid.uuid4()
        self.app_id = uuid.uuid4()
        self.sfx = sfx
        self.summary_calls: list[tuple[str, str, str]] = []
        self.stack_calls: list[tuple[str, str]] = []
        self.summary_status = "OK"
        self.stack_status = "OK"
        self.budget_after: int | None = None


@pytest_asyncio.fixture
async def ctx(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ctx]:
    c = Ctx()
    monkeypatch.setattr(settings, "github_token", "test-token")
    async with TestSession() as db:
        db.add(SpProduct(id=c.product_id, name=f"zz-p-{c.sfx}", area="CORE"))
        await db.flush()
        db.add(
            SpApplication(
                id=c.app_id,
                product_id=c.product_id,
                name=f"zz-a-{c.sfx}",
                product_key=f"zz-k-{c.sfx}",
                repository=c.repo,
                runtime="AKS",
                ci="GHA",
            )
        )
        await db.commit()
    yield c
    async with TestSession() as db:
        for model in (SpChangeSummary, SpTechStack):
            await db.execute(delete(model).where(model.repository == c.repo))
        await db.execute(delete(SpApplication).where(SpApplication.id == c.app_id))
        await db.execute(delete(SpProduct).where(SpProduct.id == c.product_id))
        await db.commit()


def _fakes(c: Ctx) -> Any:
    async def fake_summary(gh: Any, repository: str, frm: str, to: str) -> ChangeResult:
        if c.budget_after is not None and len(c.summary_calls) >= c.budget_after:
            raise GitHubBudgetExhaustedError("budget")
        c.summary_calls.append((repository, frm, to))
        if c.summary_status == "BOOM":
            raise httpx.ReadTimeout("x" * 600)
        return ChangeResult(
            status=c.summary_status,
            direction="forward" if c.summary_status == "OK" else None,
            error="err" if c.summary_status == "ERROR" else None,
            total_commits=1,
        )

    async def fake_stack(gh: Any, repository: str, version: str) -> StackResult:
        c.stack_calls.append((repository, version))
        return StackResult(status=c.stack_status, language="JAVA", version="17")

    return (
        patch.object(svc, "fetch_change_summary", fake_summary),
        patch.object(svc, "detect_stack", fake_stack),
    )


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))


async def _event(c: Ctx, target: str, prev: str | None, ver: str, at: datetime) -> None:
    async with TestSession() as db:
        db.add(
            SpDeployEvent(
                application_id=c.app_id,
                target=target,
                previous_version=prev,
                version=ver,
                detected_at=at,
                is_baseline=prev is None,
            )
        )
        await db.commit()


async def _repo_state(c: Ctx, version: str) -> None:
    async with TestSession() as db:
        await db.execute(
            delete(SpVersionState).where(
                SpVersionState.application_id == c.app_id, SpVersionState.target == "REPO"
            )
        )
        db.add(
            SpVersionState(
                application_id=c.app_id,
                target="REPO",
                version=version,
                read_status="OK",
                checked_at=T0,
            )
        )
        await db.commit()


async def _run(c: Ctx) -> dict[str, int]:
    p1, p2 = _fakes(c)
    async with TestSession() as db:
        with p1, p2:
            return await svc.run_enrichment(db, client=_client())


async def _summaries(c: Ctx) -> list[SpChangeSummary]:
    async with TestSession() as db:
        rows = await db.scalars(select(SpChangeSummary).where(SpChangeSummary.repository == c.repo))
        return list(rows)


@pytest.mark.anyio
async def test_same_pair_in_two_environments_is_one_row_and_one_call(ctx: Ctx) -> None:
    await _event(ctx, "UAT", "1.0.0", "1.1.0", T0)
    await _event(ctx, "PROD", "1.0.0", "1.1.0", T0 + timedelta(hours=1))
    counters = await _run(ctx)
    assert counters["summaries"] == 1
    assert ctx.summary_calls == [(ctx.repo, "1.0.0", "1.1.0")]
    assert len(await _summaries(ctx)) == 1


@pytest.mark.anyio
async def test_baseline_events_are_skipped(ctx: Ctx) -> None:
    await _event(ctx, "DEV", None, "1.0.0", T0)
    counters = await _run(ctx)
    assert counters["summaries"] == 0
    assert ctx.summary_calls == []


@pytest.mark.anyio
async def test_ok_and_unavailable_rows_are_not_refetched(ctx: Ctx) -> None:
    await _event(ctx, "DEV", "1.0.0", "1.1.0", T0)
    await _event(ctx, "DEV", "1.1.0", "1.2.0", T0 + timedelta(hours=1))
    ctx.summary_status = "UNAVAILABLE"
    await _run(ctx)
    ctx.summary_calls.clear()
    ctx.summary_status = "OK"
    counters = await _run(ctx)
    assert counters["summaries"] == 0
    assert ctx.summary_calls == []


@pytest.mark.anyio
async def test_error_rows_retried_until_three_attempts(ctx: Ctx) -> None:
    await _event(ctx, "DEV", "1.0.0", "1.1.0", T0)
    ctx.summary_status = "ERROR"
    await _run(ctx)
    async with TestSession() as db:
        db.add(
            SpChangeSummary(
                repository=ctx.repo, from_version="9", to_version="10", status="ERROR", attempts=2
            )
        )
        await db.execute(
            text(
                "UPDATE sp_change_summaries SET attempts = 2 "
                "WHERE repository = :r AND from_version = '1.0.0'"
            ),
            {"r": ctx.repo},
        )
        await db.commit()
    await _run(ctx)
    rows = {r.from_version: r for r in await _summaries(ctx)}
    assert rows["1.0.0"].attempts == 3
    ctx.summary_calls.clear()
    await _run(ctx)
    assert ctx.summary_calls == []


@pytest.mark.anyio
async def test_unexpected_exception_becomes_error_row_and_batch_continues(ctx: Ctx) -> None:
    await _event(ctx, "DEV", "1.0.0", "1.1.0", T0)
    await _event(ctx, "DEV", "1.1.0", "1.2.0", T0 + timedelta(hours=1))
    ctx.summary_status = "BOOM"
    counters = await _run(ctx)
    assert counters["summaries"] == 2
    rows = await _summaries(ctx)
    assert all(r.status == "ERROR" and r.attempts == 1 for r in rows)
    assert all(r.error is not None and len(r.error) <= 500 for r in rows)


@pytest.mark.anyio
async def test_batch_limit_and_newest_first(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "status_page_enrich_batch", 2)
    for i in range(4):
        await _event(ctx, "DEV", f"1.{i}.0", f"1.{i + 1}.0", T0 + timedelta(hours=i))
    counters = await _run(ctx)
    assert counters["summaries"] == 2
    assert ctx.summary_calls == [(ctx.repo, "1.3.0", "1.4.0"), (ctx.repo, "1.2.0", "1.3.0")]


@pytest.mark.anyio
async def test_budget_exhaustion_keeps_work_and_leaves_rest_pending(ctx: Ctx) -> None:
    await _event(ctx, "DEV", "1.0.0", "1.1.0", T0)
    await _event(ctx, "DEV", "1.1.0", "1.2.0", T0 + timedelta(hours=1))
    await _repo_state(ctx, "1.2.0")
    ctx.budget_after = 1
    counters = await _run(ctx)
    assert counters == {"summaries": 1, "stacks": 0, "stopped_by_budget": 1}
    assert len(await _summaries(ctx)) == 1
    ctx.budget_after = None
    counters = await _run(ctx)
    assert counters == {"summaries": 1, "stacks": 1, "stopped_by_budget": 0}
    assert len(await _summaries(ctx)) == 2


@pytest.mark.anyio
async def test_stack_refreshed_only_when_repo_version_changes(ctx: Ctx) -> None:
    await _repo_state(ctx, "1.0.0")
    ctx.stack_status = "ERROR"
    await _run(ctx)
    async with TestSession() as db:
        row = await db.get(SpTechStack, ctx.repo)
        assert row is not None
        assert row.attempts == 1
    await _repo_state(ctx, "1.1.0")
    ctx.stack_status = "OK"
    await _run(ctx)
    async with TestSession() as db:
        row = await db.get(SpTechStack, ctx.repo)
        assert row is not None
        assert (row.status, row.for_version, row.attempts) == ("OK", "1.1.0", 0)
    ctx.stack_calls.clear()
    counters = await _run(ctx)
    assert counters["stacks"] == 0
    assert ctx.stack_calls == []


@pytest.mark.anyio
async def test_no_token_makes_no_calls(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_token", "")
    await _event(ctx, "DEV", "1.0.0", "1.1.0", T0)
    counters = await _run(ctx)
    assert counters == {"summaries": 0, "stacks": 0, "stopped_by_budget": 0}
    assert ctx.summary_calls == []


@pytest.mark.anyio
async def test_lock_held_skips_run(ctx: Ctx) -> None:
    await _event(ctx, "DEV", "1.0.0", "1.1.0", T0)
    async with TestSession() as other:
        conn = await other.connection()
        await conn.execute(select(func.pg_advisory_lock(svc.ENRICH_LOCK_ID)))
        try:
            counters = await _run(ctx)
        finally:
            await conn.execute(select(func.pg_advisory_unlock(svc.ENRICH_LOCK_ID)))
    assert counters == {"summaries": 0, "stacks": 0, "stopped_by_budget": 0}
    assert ctx.summary_calls == []


@pytest.mark.anyio
async def test_shared_repository_with_different_versions_is_detected_once(ctx: Ctx) -> None:
    other_id = uuid.uuid4()
    async with TestSession() as db:
        db.add(
            SpApplication(
                id=other_id,
                product_id=ctx.product_id,
                name=f"zz-b-{ctx.sfx}",
                product_key=f"zz-kb-{ctx.sfx}",
                repository=ctx.repo,
                runtime="AKS",
                ci="GHA",
            )
        )
        await db.flush()
        db.add(
            SpVersionState(
                application_id=other_id,
                target="REPO",
                version="1.10.0",
                read_status="OK",
                checked_at=T0 + timedelta(hours=1),
            )
        )
        await db.commit()
    try:
        await _repo_state(ctx, "1.9.0")
        await _run(ctx)
        assert len(ctx.stack_calls) == 1
        assert ctx.stack_calls[0][1] == "1.10.0"
        counters = await _run(ctx)
        assert counters["stacks"] == 0
        assert len(ctx.stack_calls) == 1
    finally:
        async with TestSession() as db:
            await db.execute(delete(SpApplication).where(SpApplication.id == other_id))
            await db.commit()


@pytest.mark.anyio
async def test_db_failure_on_one_item_becomes_error_row_and_run_continues(ctx: Ctx) -> None:
    await _event(ctx, "DEV", "1.0.0", "1.1.0", T0)
    await _event(ctx, "DEV", "1.1.0", "1.2.0", T0 + timedelta(hours=1))
    p1, p2 = _fakes(ctx)

    async def fake_summary(gh: Any, repository: str, frm: str, to: str) -> ChangeResult:
        ctx.summary_calls.append((repository, frm, to))
        ref = "x" * 200 if to == "1.2.0" else "v1.1.0"
        return ChangeResult(status="OK", direction="forward", to_ref=ref, total_commits=1)

    async with TestSession() as db:
        with patch.object(svc, "fetch_change_summary", fake_summary), p2:
            counters = await svc.run_enrichment(db, client=_client())
    assert counters["summaries"] == 2
    rows = {r.to_version: r for r in await _summaries(ctx)}
    assert rows["1.2.0"].status == "ERROR"
    assert rows["1.2.0"].attempts == 1
    assert rows["1.1.0"].status == "OK"
    del p1

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.db import engine as app_engine
from app.models.status_page import (
    SpApplication,
    SpChangeSummary,
    SpDeployEvent,
    SpTechStack,
    SpVersionState,
)
from app.services.status_page_changes import fetch_change_summary
from app.services.status_page_github import EnrichGitHub, GitHubBudgetExhaustedError
from app.services.status_page_stack import StackResult, detect_stack

logger = structlog.get_logger(__name__)

ENRICH_LOCK_ID = 672
MAX_ATTEMPTS = 3
MAX_ERROR_LENGTH = 500


def _zeros() -> dict[str, int]:
    return {"summaries": 0, "stacks": 0, "stopped_by_budget": 0}


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]


async def _pending_pairs(db: AsyncSession, limit: int) -> list[tuple[str, str, str]]:
    newest = func.max(SpDeployEvent.detected_at)
    stmt = (
        select(SpApplication.repository, SpDeployEvent.previous_version, SpDeployEvent.version)
        .join(SpApplication, SpApplication.id == SpDeployEvent.application_id)
        .outerjoin(
            SpChangeSummary,
            and_(
                SpChangeSummary.repository == SpApplication.repository,
                SpChangeSummary.from_version == SpDeployEvent.previous_version,
                SpChangeSummary.to_version == SpDeployEvent.version,
            ),
        )
        .where(
            SpApplication.repository.is_not(None),
            SpDeployEvent.previous_version.is_not(None),
            SpDeployEvent.version.is_not(None),
            or_(
                SpChangeSummary.id.is_(None),
                and_(
                    SpChangeSummary.status == "ERROR",
                    SpChangeSummary.attempts < MAX_ATTEMPTS,
                ),
            ),
        )
        .group_by(SpApplication.repository, SpDeployEvent.previous_version, SpDeployEvent.version)
        .order_by(newest.desc())
        .limit(limit)
    )
    return [(r, p, v) for r, p, v in (await db.execute(stmt)).all() if r and p and v]


async def _pending_stacks(db: AsyncSession, limit: int) -> list[tuple[str, str]]:
    if limit <= 0:
        return []
    repo_version = func.max(SpVersionState.version)
    stmt = (
        select(SpApplication.repository, repo_version)
        .join(SpVersionState, SpVersionState.application_id == SpApplication.id)
        .outerjoin(SpTechStack, SpTechStack.repository == SpApplication.repository)
        .where(
            SpApplication.repository.is_not(None),
            SpVersionState.target == "REPO",
            SpVersionState.version.is_not(None),
            or_(
                SpTechStack.repository.is_(None),
                SpTechStack.for_version != SpVersionState.version,
                and_(SpTechStack.status == "ERROR", SpTechStack.attempts < MAX_ATTEMPTS),
            ),
        )
        .group_by(SpApplication.repository)
        .order_by(SpApplication.repository)
        .limit(limit)
    )
    return [(r, v) for r, v in (await db.execute(stmt)).all() if r and v]


async def _store_summary(
    db: AsyncSession, repository: str, from_version: str, to_version: str, gh: EnrichGitHub
) -> None:
    row = await db.scalar(
        select(SpChangeSummary).where(
            SpChangeSummary.repository == repository,
            SpChangeSummary.from_version == from_version,
            SpChangeSummary.to_version == to_version,
        )
    )
    error: str | None
    try:
        result = await fetch_change_summary(gh, repository, from_version, to_version)
    except GitHubBudgetExhaustedError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("status_page_summary_failed", repository=repository, error=str(exc))
        status, error = "ERROR", _error_text(exc)
        fields: dict[str, Any] = {
            "direction": None,
            "from_ref": None,
            "to_ref": None,
            "items": [],
            "total_commits": 0,
            "truncated": False,
        }
    else:
        status, error = result.status, result.error
        fields = {
            "direction": result.direction,
            "from_ref": result.from_ref,
            "to_ref": result.to_ref,
            "items": result.items,
            "total_commits": result.total_commits or 0,
            "truncated": result.truncated,
        }
    if error is not None:
        error = error[:MAX_ERROR_LENGTH]
    if row is None:
        row = SpChangeSummary(
            repository=repository, from_version=from_version, to_version=to_version, attempts=0
        )
        db.add(row)
    row.status = status
    row.error = error
    for name, value in fields.items():
        setattr(row, name, value)
    if status == "ERROR":
        row.attempts = (row.attempts or 0) + 1
    row.fetched_at = datetime.now(UTC)
    await db.commit()


async def _store_stack(
    db: AsyncSession, repository: str, repo_version: str, gh: EnrichGitHub
) -> None:
    row = await db.get(SpTechStack, repository)
    try:
        result = await detect_stack(gh, repository, repo_version)
    except GitHubBudgetExhaustedError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("status_page_stack_failed", repository=repository, error=str(exc))
        result = StackResult(status="ERROR", error=_error_text(exc))
    if row is None:
        row = SpTechStack(repository=repository, attempts=0)
        db.add(row)
    elif row.for_version != repo_version:
        row.attempts = 0
    row.for_version = repo_version
    row.status = result.status
    row.language = result.language
    row.version = result.version
    row.source = result.source
    row.ref = result.ref
    row.error = result.error[:MAX_ERROR_LENGTH] if result.error else None
    if result.status == "ERROR":
        row.attempts = (row.attempts or 0) + 1
    row.detected_at = datetime.now(UTC)
    await db.commit()


async def _process(db: AsyncSession, gh: EnrichGitHub) -> dict[str, int]:
    counters = _zeros()
    batch = settings.status_page_enrich_batch
    try:
        for repository, from_version, to_version in await _pending_pairs(db, batch):
            await _store_summary(db, repository, from_version, to_version, gh)
            counters["summaries"] += 1
        remaining = batch - counters["summaries"]
        for repository, repo_version in await _pending_stacks(db, remaining):
            await _store_stack(db, repository, repo_version, gh)
            counters["stacks"] += 1
    except GitHubBudgetExhaustedError:
        await db.rollback()
        counters["stopped_by_budget"] = 1
        logger.warning("status_page_enrichment_stopped_by_budget", **counters)
    return counters


async def run_enrichment(
    db: AsyncSession, *, client: httpx.AsyncClient | None = None
) -> dict[str, int]:
    if not settings.github_token:
        logger.info("run_enrichment skipped: no GitHub token configured")
        return _zeros()
    bind = db.bind
    lock_engine = bind if bind is not None and hasattr(bind, "connect") else app_engine
    async with lock_engine.connect() as lock_conn:
        acquired = bool(await lock_conn.scalar(select(func.pg_try_advisory_lock(ENRICH_LOCK_ID))))
        if not acquired:
            logger.info("run_enrichment skipped: another enrichment holds the lock")
            return _zeros()
        try:
            if client is not None:
                return await _process(db, EnrichGitHub(client, settings.status_page_github_reserve))
            async with httpx.AsyncClient(timeout=settings.status_page_http_timeout_s) as own:
                return await _process(db, EnrichGitHub(own, settings.status_page_github_reserve))
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": ENRICH_LOCK_ID})
            await lock_conn.commit()


__all__ = ["ENRICH_LOCK_ID", "run_enrichment"]

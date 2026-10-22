from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.e2e import E2eRun, E2eSuite
from app.services.github import GitHubClient

logger = structlog.get_logger(__name__)

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2}$")


def derive_status(passed: int, failed: int) -> str:
    try:
        if failed > 0:
            return "failed"
        if passed > 0:
            return "passed"
        return "mixed"
    except Exception:
        logger.exception("derive_status failed")
        raise


def parse_run_at(timestamp: str) -> datetime:
    try:
        return datetime.strptime(timestamp, "%Y-%m-%d_%H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        logger.exception("parse_run_at failed")
        raise


def duration_to_ms(duration: str) -> int:
    try:
        h, m, s = (int(x) for x in duration.split(":"))
        ms = (h * 3600 + m * 60 + s) * 1000
        return max(0, ms)
    except Exception:
        logger.exception("duration_to_ms failed")
        raise


def build_allure_url(github_repo: str, suite_path: str, timestamp: str) -> str:
    try:
        owner, repo = github_repo.split("/", 1)
        return f"https://{owner}.github.io/{repo}/{suite_path}/{timestamp}/index.html"
    except Exception:
        logger.exception("build_allure_url failed")
        raise


async def sync_suite(suite: E2eSuite, db: AsyncSession) -> int:
    try:
        if not settings.github_token:
            raise ValueError("GITHUB_TOKEN is not configured")

        client = GitHubClient(token=settings.github_token, repo=suite.github_repo)
        entries = await client.list_directory(suite.suite_path)

        cutoff: datetime | None = None
        if suite.sync_lookback_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=suite.sync_lookback_days)

        new_count = 0
        for entry in entries:
            if entry.get("type") != "dir":
                continue
            name: str = entry["name"]
            if not TIMESTAMP_RE.match(name):
                continue

            run_at = parse_run_at(name)

            if cutoff is not None and run_at < cutoff:
                continue

            already_synced = await db.scalar(
                select(E2eRun).where(E2eRun.suite_id == suite.id, E2eRun.run_at == run_at)
            )
            if already_synced is None or not already_synced:
                logger.info("sync_suite query succeeded with 0 results")
            if already_synced:
                continue

            raw = await client.get_file_content(f"{suite.suite_path}/{name}/stats.json")
            stats = json.loads(raw)

            passed = int(stats.get("passed", 0))
            failed = int(stats.get("failed", 0))
            skipped = int(stats.get("skipped", 0))
            duration_ms = duration_to_ms(stats.get("duration", "00:00:00"))

            db.add(
                E2eRun(
                    suite_id=suite.id,
                    run_at=run_at,
                    passed=passed,
                    failed=failed,
                    skipped=skipped,
                    duration_ms=duration_ms,
                    allure_url=build_allure_url(suite.github_repo, suite.suite_path, name),
                    status=derive_status(passed, failed),
                )
            )
            new_count += 1

        suite.last_synced_at = datetime.now(timezone.utc)
        await db.commit()
        return new_count
    except Exception:
        logger.exception("sync_suite failed")
        raise


TREND_WINDOW = 10


async def list_suites_with_latest_run(
    db: AsyncSession,
) -> list[tuple[E2eSuite, E2eRun | None, list[float]]]:
    try:
        suites = list(await db.scalars(select(E2eSuite).order_by(E2eSuite.display_name)))
        if suites is None or not suites:
            logger.info("list_suites_with_latest_run query succeeded with 0 results")
        result: list[tuple[E2eSuite, E2eRun | None, list[float]]] = []
        for suite in suites:
            rows = list(await db.scalars(
                select(E2eRun)
                .where(E2eRun.suite_id == suite.id)
                .order_by(E2eRun.run_at.desc())
                .limit(TREND_WINDOW)
            ))
            if rows is None or not rows:
                logger.info("list_suites_with_latest_run query succeeded with 0 results")
            latest = rows[0] if rows else None
            trend = []
            for run in reversed(rows):
                total = run.passed + run.failed + run.skipped
                trend.append(round(run.passed / total, 4) if total > 0 else 0.0)
            result.append((suite, latest, trend))
        return result
    except Exception:
        logger.exception("list_suites_with_latest_run failed")
        raise


async def create_suite(db: AsyncSession, name: str, display_name: str, suite_path: str, github_repo: str, enabled: bool, sync_lookback_days: int | None = None) -> E2eSuite:
    try:
        suite = E2eSuite(
            name=name,
            display_name=display_name,
            suite_path=suite_path,
            github_repo=github_repo,
            enabled=enabled,
            sync_lookback_days=sync_lookback_days,
        )
        db.add(suite)
        await db.commit()
        await db.refresh(suite)
        return suite
    except Exception:
        logger.exception("create_suite failed")
        raise


async def update_suite(db: AsyncSession, suite: E2eSuite, fields: dict) -> E2eSuite:
    try:
        for key, value in fields.items():
            setattr(suite, key, value)
        await db.commit()
        await db.refresh(suite)
        return suite
    except Exception:
        logger.exception("update_suite failed")
        raise


async def get_suite(db: AsyncSession, suite_id: UUID) -> E2eSuite | None:
    try:
        _query_result_for_logging = await db.scalar(select(E2eSuite).where(E2eSuite.id == suite_id))
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("get_suite query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("get_suite failed")
        raise


async def get_runs(
    db: AsyncSession,
    suite_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[tuple[E2eRun, str, str]], int]:
    """Returns list of (run, suite_name, suite_display_name) and total count."""
    try:
        base = (
            select(E2eRun, E2eSuite.name, E2eSuite.display_name)
            .join(E2eSuite, E2eRun.suite_id == E2eSuite.id)
        )
        if suite_id is not None:
            base = base.where(E2eRun.suite_id == suite_id)

        count_q = select(func.count()).select_from(base.subquery())
        total: int = await db.scalar(count_q) or 0
        if total is None or not total:
            logger.info("get_runs query succeeded with 0 results")

        rows = await db.execute(
            base.order_by(E2eRun.run_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        _query_result_for_logging = list(rows.all()), total
        if _query_result_for_logging[0] is None or not _query_result_for_logging[0]:
            logger.info("get_runs query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("get_runs failed")
        raise


async def get_run(db: AsyncSession, run_id: UUID) -> E2eRun | None:
    try:
        _query_result_for_logging = await db.scalar(select(E2eRun).where(E2eRun.id == run_id))
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("get_run query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("get_run failed")
        raise


async def delete_run(db: AsyncSession, run: E2eRun) -> None:
    try:
        await db.delete(run)
        await db.commit()
    except Exception:
        logger.exception("delete_run failed")
        raise


async def delete_runs_bulk(db: AsyncSession, run_ids: list[UUID]) -> int:
    try:
        from sqlalchemy import delete
        result = await db.execute(
            delete(E2eRun).where(E2eRun.id.in_(run_ids))
        )
        await db.commit()
        _query_result_for_logging = result.rowcount
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("delete_runs_bulk query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("delete_runs_bulk failed")
        raise

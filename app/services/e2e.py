from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.e2e import E2eRun, E2eSuite
from app.services.github import GitHubClient

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2}$")


def derive_status(passed: int, failed: int) -> str:
    if failed > 0:
        return "failed"
    if passed > 0:
        return "passed"
    return "mixed"


def parse_run_at(timestamp: str) -> datetime:
    return datetime.strptime(timestamp, "%Y-%m-%d_%H:%M:%S").replace(tzinfo=timezone.utc)


def duration_to_ms(duration: str) -> int:
    h, m, s = (int(x) for x in duration.split(":"))
    ms = (h * 3600 + m * 60 + s) * 1000
    return max(0, ms)


def build_allure_url(github_repo: str, suite_path: str, timestamp: str) -> str:
    owner, repo = github_repo.split("/", 1)
    return f"https://{owner}.github.io/{repo}/{suite_path}/{timestamp}/index.html"


async def sync_suite(suite: E2eSuite, db: AsyncSession) -> int:
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


TREND_WINDOW = 10


async def list_suites_with_latest_run(
    db: AsyncSession,
) -> list[tuple[E2eSuite, E2eRun | None, list[float]]]:
    suites = list(await db.scalars(select(E2eSuite).order_by(E2eSuite.display_name)))
    result: list[tuple[E2eSuite, E2eRun | None, list[float]]] = []
    for suite in suites:
        rows = list(await db.scalars(
            select(E2eRun)
            .where(E2eRun.suite_id == suite.id)
            .order_by(E2eRun.run_at.desc())
            .limit(TREND_WINDOW)
        ))
        latest = rows[0] if rows else None
        trend = []
        for run in reversed(rows):
            total = run.passed + run.failed + run.skipped
            trend.append(round(run.passed / total, 4) if total > 0 else 0.0)
        result.append((suite, latest, trend))
    return result


async def create_suite(db: AsyncSession, name: str, display_name: str, suite_path: str, github_repo: str, enabled: bool, sync_lookback_days: int | None = None) -> E2eSuite:
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


async def update_suite(db: AsyncSession, suite: E2eSuite, fields: dict) -> E2eSuite:
    for key, value in fields.items():
        setattr(suite, key, value)
    await db.commit()
    await db.refresh(suite)
    return suite


async def get_suite(db: AsyncSession, suite_id: UUID) -> E2eSuite | None:
    return await db.scalar(select(E2eSuite).where(E2eSuite.id == suite_id))


async def get_runs(
    db: AsyncSession,
    suite_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[tuple[E2eRun, str, str]], int]:
    """Returns list of (run, suite_name, suite_display_name) and total count."""
    base = (
        select(E2eRun, E2eSuite.name, E2eSuite.display_name)
        .join(E2eSuite, E2eRun.suite_id == E2eSuite.id)
    )
    if suite_id is not None:
        base = base.where(E2eRun.suite_id == suite_id)

    count_q = select(func.count()).select_from(base.subquery())
    total: int = await db.scalar(count_q) or 0

    rows = await db.execute(
        base.order_by(E2eRun.run_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(rows.all()), total


async def get_run(db: AsyncSession, run_id: UUID) -> E2eRun | None:
    return await db.scalar(select(E2eRun).where(E2eRun.id == run_id))


async def delete_run(db: AsyncSession, run: E2eRun) -> None:
    await db.delete(run)
    await db.commit()


async def delete_runs_bulk(db: AsyncSession, run_ids: list[UUID]) -> int:
    from sqlalchemy import delete
    result = await db.execute(
        delete(E2eRun).where(E2eRun.id.in_(run_ids))
    )
    await db.commit()
    return result.rowcount

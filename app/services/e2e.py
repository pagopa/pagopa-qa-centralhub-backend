from __future__ import annotations

import json
import re
from datetime import datetime, timezone
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
    return (h * 3600 + m * 60 + s) * 1000


def build_allure_url(github_repo: str, suite_path: str, timestamp: str) -> str:
    owner, repo = github_repo.split("/", 1)
    return f"https://{owner}.github.io/{repo}/{suite_path}/{timestamp}/index.html"


async def sync_suite(suite: E2eSuite, db: AsyncSession) -> int:
    if not settings.github_token:
        raise ValueError("GITHUB_TOKEN is not configured")

    client = GitHubClient(token=settings.github_token, repo=suite.github_repo)
    entries = await client.list_directory(suite.suite_path)

    new_count = 0
    for entry in entries:
        if entry.get("type") != "dir":
            continue
        name: str = entry["name"]
        if not TIMESTAMP_RE.match(name):
            continue

        run_at = parse_run_at(name)

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


async def list_suites_with_latest_run(
    db: AsyncSession,
) -> list[tuple[E2eSuite, E2eRun | None]]:
    suites = list(await db.scalars(select(E2eSuite).order_by(E2eSuite.display_name)))
    result: list[tuple[E2eSuite, E2eRun | None]] = []
    for suite in suites:
        latest = await db.scalar(
            select(E2eRun)
            .where(E2eRun.suite_id == suite.id)
            .order_by(E2eRun.run_at.desc())
            .limit(1)
        )
        result.append((suite, latest))
    return result


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

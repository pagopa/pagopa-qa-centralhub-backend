from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.gpd_position import GpdPositionSnapshot, GpdPositionSyncStatus
from app.services.github import GitHubClient

logger = structlog.get_logger(__name__)

REPORT_LINE_RE = re.compile(r"report data (\{.*\})")

GPD_REPORT_REPO = "pagopa/pagopa-qa"
GPD_REPORT_WORKFLOW = "gpd_report.yml"
BACKFILL_DAYS = 90

FIELD_MAP = {
    "TOTAL": "total",
    "GPD": "gpd",
    "GPD_PAYABLE": "gpd_payable",
    "GPD4ACA": "gpd4aca",
    "GPD4ACA_PAYABLE": "gpd4aca_payable",
    "WISP": "wisp",
    "PA_CREATE_POSITION": "pa_create_position",
    "PA_CREATE_POSITION_PAYABLE": "pa_create_position_payable",
}


def parse_report_line(log_text: str) -> dict | None:
    match = REPORT_LINE_RE.search(log_text)
    if not match:
        return None
    try:
        data = ast.literal_eval(match.group(1))
    except (ValueError, SyntaxError):
        return None
    if not isinstance(data, dict):
        return None
    return data


async def sync_from_source(db: AsyncSession) -> int:
    if not settings.github_token:
        raise ValueError("GITHUB_TOKEN is not configured")

    client = GitHubClient(token=settings.github_token, repo=GPD_REPORT_REPO)

    existing_run_ids = set(await db.scalars(select(GpdPositionSnapshot.run_id)))
    is_first_sync = len(existing_run_ids) == 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)

    runs = await client.list_workflow_runs(GPD_REPORT_WORKFLOW)

    new_count = 0
    for run in runs:
        run_id = run["id"]
        if run_id in existing_run_ids:
            break

        created_at = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if is_first_sync and created_at < cutoff:
            break

        log_text = await client.get_job_log(run_id)
        report = parse_report_line(log_text)
        if report is None:
            logger.warning("gpd_position_report_not_found", run_id=run_id)
            continue

        try:
            fields = {FIELD_MAP[key]: int(value) for key, value in report.items() if key in FIELD_MAP}
        except (TypeError, ValueError):
            logger.warning("gpd_position_report_invalid", run_id=run_id)
            continue

        if len(fields) != len(FIELD_MAP):
            logger.warning("gpd_position_report_incomplete", run_id=run_id, fields=list(fields))
            continue

        db.add(
            GpdPositionSnapshot(
                report_date=created_at.date(),
                run_id=run_id,
                **fields,
            )
        )
        new_count += 1

    sync_status = await db.get(GpdPositionSyncStatus, 1)
    if sync_status is None:
        sync_status = GpdPositionSyncStatus(id=1)
        db.add(sync_status)
    sync_status.item_count = len(existing_run_ids) + new_count
    sync_status.synced_at = datetime.now(timezone.utc)

    await db.commit()
    return new_count


async def list_snapshots(db: AsyncSession) -> list[GpdPositionSnapshot]:
    result = await db.scalars(
        select(GpdPositionSnapshot).order_by(GpdPositionSnapshot.report_date)
    )
    return list(result)


async def get_sync_status(db: AsyncSession) -> GpdPositionSyncStatus | None:
    return await db.get(GpdPositionSyncStatus, 1)

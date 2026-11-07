from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.test_metrics import TestRun
from app.schemas.quality_gate import (
    QualityGateRunCreate,
    QualityGateRunOut,
    QualityGateRunStatus,
)
from app.services.github import GitHubClient

logger = structlog.get_logger(__name__)

CALLER_ID = "pagopa-qa-centralhub"
AUDIT_ACTION = "quality_gate.run.dispatch"
RUN_NAME_PREFIX = "tas-qg-"
RECENT_RUNS_WINDOW = timedelta(hours=1)
MAX_LISTED_RUNS = 20
DELETE_AUDIT_ACTION = "quality_gate.run.delete"


class QualityGateConfigError(Exception):
    """The launch feature is not configured on this deployment."""


class QualityGateRunNotFoundError(Exception):
    """No test run exists with the given id."""


class NotAQualityGateRunError(Exception):
    """The test run exists but was not produced by a Quality Gate."""


class QualityGateSuiteNotAllowedError(Exception):
    """The requested suite is not in the configured allowlist."""


def _allowed_suites() -> set[str]:
    return {s.strip() for s in settings.qg_launch_suites.split(",") if s.strip()}


async def _audit(
    db: AsyncSession, correlation_id: str, detail: dict, action: str = AUDIT_ACTION
) -> None:
    try:
        db.add(
            AuditLog(
                actor_id=None,
                action=action,
                resource_type="quality_gate_run",
                resource_id=correlation_id,
                detail=json.dumps(detail),
            )
        )
        await db.commit()
    except Exception:
        # An audit failure must never hide the outcome of the dispatch itself.
        logger.exception("quality_gate audit log write failed", correlation_id=correlation_id)
        await db.rollback()


async def launch_quality_gate_run(
    db: AsyncSession, body: QualityGateRunCreate
) -> QualityGateRunOut:
    if not settings.tas_github_token:
        raise QualityGateConfigError("TAS_GITHUB_TOKEN is not configured")

    allowed = _allowed_suites()
    if allowed and body.test_suite not in allowed:
        raise QualityGateSuiteNotAllowedError(body.test_suite)

    # The workflow run-name is "tas-<correlation_id>", so this yields "tas-qg-<hex>".
    correlation_id = f"qg-{uuid.uuid4().hex}"
    inputs = {
        "test_type": body.test_type,
        "test_suite": body.test_suite,
        "environment": body.environment,
        "caller_id": CALLER_ID,
        "correlation_id": correlation_id,
        "tags": body.tags,
        "allure": "true" if body.allure else "false",
        "isQualityGateRun": "true",
    }
    detail = {"inputs": inputs, "repo": settings.tas_repo, "ref": settings.tas_ref}

    client = GitHubClient(token=settings.tas_github_token, repo=settings.tas_repo)
    try:
        await client.dispatch_workflow(
            settings.tas_workflow_file, ref=settings.tas_ref, inputs=inputs
        )
    except Exception as exc:
        failure = {**detail, "outcome": "failed", "error": type(exc).__name__}
        await _audit(db, correlation_id, failure)
        raise
    await _audit(db, correlation_id, {**detail, "outcome": "dispatched"})
    logger.info("quality_gate run dispatched", correlation_id=correlation_id, suite=body.test_suite)

    return QualityGateRunOut(
        correlation_id=correlation_id,
        run_name=f"tas-{correlation_id}",
        actions_url=(
            f"https://github.com/{settings.tas_repo}/actions/workflows/"
            f"{settings.tas_workflow_file}"
        ),
    )


def _map_state(github_status: str | None) -> str:
    if github_status == "completed":
        return "completed"
    if github_status == "in_progress":
        return "in_progress"
    # queued, waiting, requested, pending: the run exists but has not started yet.
    return "queued"


def _to_status(correlation_id: str, run: dict | None) -> QualityGateRunStatus:
    run_name = f"tas-{correlation_id}"
    if run is None:
        return QualityGateRunStatus(
            correlation_id=correlation_id, run_name=run_name, state="pending"
        )
    state = _map_state(run.get("status"))
    return QualityGateRunStatus(
        correlation_id=correlation_id,
        run_name=run_name,
        state=state,  # type: ignore[arg-type]
        conclusion=run.get("conclusion") if state == "completed" else None,
        html_url=run.get("html_url"),
        created_at=run.get("created_at"),
        updated_at=run.get("updated_at"),
    )


async def get_quality_gate_run_status(correlation_id: str) -> QualityGateRunStatus:
    if not settings.tas_github_token:
        raise QualityGateConfigError("TAS_GITHUB_TOKEN is not configured")

    client = GitHubClient(token=settings.tas_github_token, repo=settings.tas_repo)
    run = await client.find_workflow_run_by_name(
        settings.tas_workflow_file, f"tas-{correlation_id}"
    )
    return _to_status(correlation_id, run)


def _is_recent(run: dict, now: datetime) -> bool:
    created = run.get("created_at")
    if not created:
        return False
    try:
        created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return False
    return now - created_at <= RECENT_RUNS_WINDOW


async def list_quality_gate_runs() -> list[QualityGateRunStatus]:
    """Quality Gate runs launched from the Hub that are active or started recently.

    GitHub is the source of truth: nothing is stored on the Hub. Runs are recognised
    by their run name (``tas-qg-<uuid>``); active runs are always listed, finished ones
    only within ``RECENT_RUNS_WINDOW`` so a run that failed before writing any report
    stays visible to the operator for a while.
    """
    if not settings.tas_github_token:
        raise QualityGateConfigError("TAS_GITHUB_TOKEN is not configured")

    client = GitHubClient(token=settings.tas_github_token, repo=settings.tas_repo)
    runs = await client.list_dispatched_runs(settings.tas_workflow_file)
    now = datetime.now(UTC)
    selected = [
        run
        for run in runs
        if str(run.get("display_title", "")).startswith(RUN_NAME_PREFIX)
        and (run.get("status") != "completed" or _is_recent(run, now))
    ]
    selected.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return [
        _to_status(str(run["display_title"])[len("tas-") :], run)
        for run in selected[:MAX_LISTED_RUNS]
    ]


async def delete_quality_gate_run(db: AsyncSession, run_id: uuid.UUID) -> None:
    """Delete a Quality Gate test run (its scenarios are removed by the FK cascade).

    Only runs whose origin is QG can be deleted: TAS and NRT history must stay untouched.
    The published Allure report is not removed.
    """
    run = (await db.execute(select(TestRun).where(TestRun.id == run_id))).scalar_one_or_none()
    if run is None:
        raise QualityGateRunNotFoundError(str(run_id))
    if run.test_origin != "QG":
        raise NotAQualityGateRunError(str(run_id))

    detail = {
        "suite_id": str(run.suite_id),
        "timestamp_start": str(run.timestamp_start),
        "allure_url": run.allure_url,
        "revision_result": run.revision_result.value if run.revision_result else None,
    }
    await db.execute(delete(TestRun).where(TestRun.id == run_id))
    await db.commit()
    await _audit(db, str(run_id), detail, action=DELETE_AUDIT_ACTION)
    logger.info("quality_gate run deleted", run_id=str(run_id))

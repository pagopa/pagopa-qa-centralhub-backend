from __future__ import annotations

import json
import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.audit_log import AuditLog
from app.schemas.quality_gate import QualityGateRunCreate, QualityGateRunOut
from app.services.github import GitHubClient

logger = structlog.get_logger(__name__)

CALLER_ID = "pagopa-qa-centralhub"
AUDIT_ACTION = "quality_gate.run.dispatch"


class QualityGateConfigError(Exception):
    """The launch feature is not configured on this deployment."""


class QualityGateSuiteNotAllowedError(Exception):
    """The requested suite is not in the configured allowlist."""


def _allowed_suites() -> set[str]:
    return {s.strip() for s in settings.qg_launch_suites.split(",") if s.strip()}


async def _audit(db: AsyncSession, correlation_id: str, detail: dict) -> None:
    try:
        db.add(
            AuditLog(
                actor_id=None,
                action=AUDIT_ACTION,
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

from __future__ import annotations

import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.sanp_health import (
    SanpEnvironment,
    SanpHealthChange,
    SanpHealthResult,
    SanpHealthRun,
    SanpHealthSyncStatus,
    SanpImportStatus,
    SanpResultStatus,
    SanpSeverity,
)
from app.schemas.sanp_health import (
    SanpHealthLatestResponse,
    SanpHealthMatrixCellOut,
    SanpHealthMatrixRowOut,
    SanpHealthReportDetailOut,
    SanpHealthRunOut,
    SanpHealthSyncResult,
)
from app.services.github import GitHubClient
from app.services.sanp_health_artifacts import (
    MAX_ARTIFACT_BYTES,
    DriftArtifactError,
    DriftArtifactPayload,
    classify_result,
    parse_drift_artifact,
)

REPOSITORY = "pagopa/pagopa-api"
WORKFLOW_FILE = "daily_api_spec_drift_detector.yaml"
RETENTION_DAYS = 7
ARTIFACT_RETRY_HOURS = 24
SYNC_LOCK_ID = 669
PAGE_SIZE = 100

SANP_VERSION_RE = re.compile(r"SANP\s*([0-9]+(?:\.[0-9]+)*)", re.IGNORECASE)
ARTIFACT_PREFIXES = ("drift-d-", "drift-u-", "drift-p-")
TARGET_APIM = {
    SanpEnvironment.SANP: "DEV",
    SanpEnvironment.COLLAUDO: "UAT",
    SanpEnvironment.PRODUZIONE: "PROD",
}


class SanpHealthSyncInProgressError(RuntimeError):
    """Raised when another SANP Health synchronization owns the advisory lock."""


@dataclass(frozen=True)
class _RunMetadata:
    github_run_id: int
    run_number: int
    head_sha: str
    workflow_branch: str
    conclusion: str
    html_url: str
    started_at: datetime
    completed_at: datetime
    synced_at: datetime


@dataclass(frozen=True)
class _CollectedRun:
    metadata: _RunMetadata
    payloads: tuple[DriftArtifactPayload, ...]
    errors: tuple[str, ...]
    artifact_listing_error: httpx.HTTPError | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _artifact_is_relevant(name: str) -> bool:
    normalized = name.lower()
    return normalized.startswith(ARTIFACT_PREFIXES) and not normalized.startswith("drift-main-")


def _summarize_errors(errors: list[str] | tuple[str, ...]) -> list[str]:
    expired_runs: Counter[str] = Counter()
    other_errors: list[str] = []
    expired_pattern = re.compile(r"^run (\d+), artifact .+: artifact expired$")

    for error in errors:
        match = expired_pattern.match(error)
        if match:
            expired_runs[match.group(1)] += 1
        else:
            other_errors.append(error)

    summaries = [
        f"run {run_id}: {count} artifacts expired"
        for run_id, count in expired_runs.items()
    ]
    summaries.extend(other_errors)
    max_summaries = 10
    if len(summaries) > max_summaries:
        omitted = len(summaries) - (max_summaries - 1)
        summaries = summaries[: max_summaries - 1]
        summaries.append(f"{omitted} additional errors omitted")
    return summaries


async def _set_sync_status(
    db: AsyncSession,
    *,
    attempted_at: datetime,
    succeeded_at: datetime | None,
    error: str | None,
    imported_run_count: int,
) -> None:
    status = await db.get(SanpHealthSyncStatus, 1)
    if status is None:
        status = SanpHealthSyncStatus(id=1, last_attempt_at=attempted_at)
        db.add(status)
    status.last_attempt_at = attempted_at
    if succeeded_at is not None:
        status.last_success_at = succeeded_at
    status.last_error = error
    status.imported_run_count = imported_run_count


async def _record_failed_sync_status(
    db: AsyncSession,
    *,
    attempted_at: datetime,
    error: str,
) -> None:
    await db.rollback()
    try:
        async with db.begin():
            await _set_sync_status(
                db,
                attempted_at=attempted_at,
                succeeded_at=None,
                error=error,
                imported_run_count=0,
            )
    except Exception:
        await db.rollback()


def _collect_run_metadata(source: dict[str, Any], synced_at: datetime) -> _RunMetadata:
    completed_value = source.get("updated_at") or source.get("completed_at")
    started_value = source.get("run_started_at") or source.get("created_at")
    if not completed_value or not started_value:
        raise ValueError(f"workflow run {source.get('id')} has incomplete timestamps")

    return _RunMetadata(
        github_run_id=int(source["id"]),
        run_number=int(source["run_number"]),
        head_sha=str(source["head_sha"]),
        workflow_branch=str(source["head_branch"]),
        conclusion=str(source.get("conclusion") or "unknown"),
        html_url=str(source["html_url"]),
        started_at=_parse_timestamp(str(started_value)),
        completed_at=_parse_timestamp(str(completed_value)),
        synced_at=synced_at,
    )


async def _collect_pages(
    fetch_page: Callable[[int, int], Awaitable[list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        current = await fetch_page(page, PAGE_SIZE)
        items.extend(current)
        if len(current) < PAGE_SIZE:
            return items
        page += 1


async def _list_workflow_runs(
    client: GitHubClient,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        current = await client.list_workflow_runs(
            WORKFLOW_FILE,
            status="completed",
            per_page=PAGE_SIZE,
            page=page,
        )
        for source in current:
            completed_value = source.get("updated_at") or source.get("completed_at")
            if completed_value and _parse_timestamp(str(completed_value)) < cutoff:
                return items
            items.append(source)
        if len(current) < PAGE_SIZE:
            return items
        page += 1


async def _list_run_artifacts(client: GitHubClient, github_run_id: int) -> list[dict[str, Any]]:
    return await _collect_pages(
        lambda page, per_page: client.list_run_artifacts(
            github_run_id,
            per_page=per_page,
            page=page,
        )
    )


async def _collect_run(client: GitHubClient, metadata: _RunMetadata) -> _CollectedRun:
    errors: list[str] = []
    payloads: list[DriftArtifactPayload] = []
    artifact_listing_error: httpx.HTTPError | None = None
    try:
        artifacts = await _list_run_artifacts(client, metadata.github_run_id)
    except httpx.HTTPError as exc:
        artifacts = []
        artifact_listing_error = exc
        errors.append(f"run {metadata.github_run_id}: artifact listing failed")
    except ValueError:
        artifacts = []
        errors.append(f"run {metadata.github_run_id}: artifact listing failed")

    relevant_artifacts = [
        artifact for artifact in artifacts if _artifact_is_relevant(str(artifact.get("name", "")))
    ]
    if not relevant_artifacts and not errors:
        errors.append(f"run {metadata.github_run_id}: no drift artifacts found")

    for artifact in relevant_artifacts:
        name = str(artifact.get("name", ""))
        prefix = f"run {metadata.github_run_id}, artifact {name}:"
        if artifact.get("expired"):
            errors.append(f"{prefix} artifact expired")
            continue
        try:
            content = await client.download_artifact(
                int(artifact["id"]), max_bytes=MAX_ARTIFACT_BYTES
            )
            payload = parse_drift_artifact(name, content)
        except (DriftArtifactError, KeyError, TypeError):
            errors.append(f"{prefix} invalid artifact")
            continue
        except (httpx.HTTPError, ValueError):
            errors.append(f"{prefix} artifact download failed")
            continue
        if payload is not None:
            payloads.append(payload)

    return _CollectedRun(
        metadata=metadata,
        payloads=tuple(payloads),
        errors=tuple(errors),
        artifact_listing_error=artifact_listing_error,
    )


async def _upsert_result(
    db: AsyncSession,
    run: SanpHealthRun,
    payload: DriftArtifactPayload,
) -> None:
    environment = SanpEnvironment(payload.env)
    result = await db.scalar(
        select(SanpHealthResult).where(
            SanpHealthResult.run_id == run.id,
            SanpHealthResult.spec_name == payload.file,
            SanpHealthResult.environment == environment,
        )
    )
    if result is None:
        result = SanpHealthResult(
            run_id=run.id,
            spec_name=payload.file,
            environment=environment,
            source_branch=payload.branch,
            target_apim=TARGET_APIM[environment],
            display_name=payload.display_name,
            description=payload.description,
            status=SanpResultStatus.OK,
            error_count=0,
            warning_count=0,
            info_count=0,
            raw_payload={},
        )
        db.add(result)
        await db.flush()
    else:
        await db.execute(delete(SanpHealthChange).where(SanpHealthChange.result_id == result.id))

    result.source_branch = payload.branch
    result.target_apim = TARGET_APIM[environment]
    result.display_name = payload.display_name
    result.description = payload.description
    result.status = SanpResultStatus(classify_result(payload.changes))
    result.error_count = sum(change.level == 3 for change in payload.changes)
    result.warning_count = sum(change.level == 2 for change in payload.changes)
    result.info_count = sum(change.level == 1 for change in payload.changes)
    result.raw_payload = payload.model_dump(mode="json")

    for order, change in enumerate(payload.changes):
        db.add(
            SanpHealthChange(
                result_id=result.id,
                level=change.level,
                severity=SanpSeverity(change.severity),
                rule_id=change.id,
                message=change.text,
                path=change.path,
                operation=change.operation,
                section=change.section,
                comment=change.comment,
                change_order=order,
            )
        )
    await db.flush()


async def _refresh_run_counts(db: AsyncSession, run: SanpHealthRun) -> None:
    counts = (
        await db.execute(
            select(
                func.coalesce(func.sum(SanpHealthResult.error_count), 0),
                func.coalesce(func.sum(SanpHealthResult.warning_count), 0),
                func.coalesce(func.sum(SanpHealthResult.info_count), 0),
            ).where(SanpHealthResult.run_id == run.id)
        )
    ).one()
    run.error_count = int(counts[0])
    run.warning_count = int(counts[1])
    run.info_count = int(counts[2])


async def _persist_run(
    db: AsyncSession,
    collected: _CollectedRun,
) -> SanpImportStatus:
    metadata = collected.metadata
    run = await db.scalar(
        select(SanpHealthRun).where(SanpHealthRun.github_run_id == metadata.github_run_id)
    )
    if run is None:
        run = SanpHealthRun(
            github_run_id=metadata.github_run_id,
            run_number=metadata.run_number,
            head_sha=metadata.head_sha,
            workflow_branch=metadata.workflow_branch,
            conclusion=metadata.conclusion,
            html_url=metadata.html_url,
            started_at=metadata.started_at,
            completed_at=metadata.completed_at,
            synced_at=metadata.synced_at,
            import_status=SanpImportStatus.FAILED,
        )
        db.add(run)
        await db.flush()

    run.run_number = metadata.run_number
    run.head_sha = metadata.head_sha
    run.workflow_branch = metadata.workflow_branch
    run.conclusion = metadata.conclusion
    run.html_url = metadata.html_url
    run.started_at = metadata.started_at
    run.completed_at = metadata.completed_at
    run.synced_at = metadata.synced_at

    for payload in collected.payloads:
        async with db.begin_nested():
            await _upsert_result(db, run, payload)

    if not collected.payloads:
        run.import_status = SanpImportStatus.FAILED
    elif collected.errors:
        run.import_status = SanpImportStatus.PARTIAL
    else:
        run.import_status = SanpImportStatus.COMPLETE
    run.error_message = "; ".join(_summarize_errors(collected.errors)) or None
    await _refresh_run_counts(db, run)
    return run.import_status


async def sync_from_source(db: AsyncSession) -> SanpHealthSyncResult:
    """Synchronize retained completed workflow runs and return this attempt's outcome."""
    attempted_at = _utcnow()
    if not settings.github_token:
        await _record_failed_sync_status(
            db,
            attempted_at=attempted_at,
            error="GitHub token is not configured",
        )
        raise ValueError("GITHUB_TOKEN is not configured")

    client = GitHubClient(token=settings.github_token, repo=REPOSITORY)
    try:
        cutoff = attempted_at - timedelta(days=RETENTION_DAYS)
        runs = await _list_workflow_runs(client, cutoff)
        metadata = [
            item
            for source in runs
            if (item := _collect_run_metadata(source, attempted_at)).completed_at >= cutoff
        ]
        artifact_retry_cutoff = attempted_at - timedelta(hours=ARTIFACT_RETRY_HOURS)
        metadata = [item for item in metadata if item.completed_at >= artifact_retry_cutoff]

        complete_run_ids: set[int] = set()
        if metadata:
            complete_run_ids = set(
                await db.scalars(
                    select(SanpHealthRun.github_run_id).where(
                        SanpHealthRun.github_run_id.in_(item.github_run_id for item in metadata),
                        SanpHealthRun.import_status == SanpImportStatus.COMPLETE,
                    )
                )
            )
            await db.rollback()

        collected_runs = [
            await _collect_run(client, item)
            for item in metadata
            if item.github_run_id not in complete_run_ids
        ]
        if collected_runs and all(
            collected.artifact_listing_error is not None for collected in collected_runs
        ):
            artifact_listing_error = collected_runs[0].artifact_listing_error
            if artifact_listing_error is not None:
                raise artifact_listing_error
    except (httpx.HTTPError, ValueError):
        await _record_failed_sync_status(
            db,
            attempted_at=attempted_at,
            error="GitHub request failed",
        )
        raise
    except Exception:
        await _record_failed_sync_status(
            db,
            attempted_at=attempted_at,
            error="SANP Health synchronization failed",
        )
        raise

    run_statuses: list[SanpImportStatus] = []
    errors: list[str] = []
    imported_run_count = 0
    try:
        async with db.begin():
            lock_acquired = bool(
                await db.scalar(select(func.pg_try_advisory_xact_lock(SYNC_LOCK_ID)))
            )
            if not lock_acquired:
                raise SanpHealthSyncInProgressError(
                    "SANP Health synchronization is already running"
                )

            if collected_runs:
                completed_run_ids = set(
                    await db.scalars(
                        select(SanpHealthRun.github_run_id).where(
                            SanpHealthRun.github_run_id.in_(
                                collected.metadata.github_run_id for collected in collected_runs
                            ),
                            SanpHealthRun.import_status == SanpImportStatus.COMPLETE,
                        )
                    )
                )
                collected_runs = [
                    collected
                    for collected in collected_runs
                    if collected.metadata.github_run_id not in completed_run_ids
                ]

            errors = _summarize_errors(
                [error for collected in collected_runs for error in collected.errors]
            )
            for collected in collected_runs:
                try:
                    async with db.begin_nested():
                        run_status = await _persist_run(db, collected)
                except Exception:
                    run_statuses.append(SanpImportStatus.FAILED)
                    errors.append(f"run {collected.metadata.github_run_id}: persistence failed")
                else:
                    run_statuses.append(run_status)
                    imported_run_count += 1

            errors = _summarize_errors(errors)

            if not run_statuses or any(
                status is not SanpImportStatus.FAILED for status in run_statuses
            ):
                await db.execute(delete(SanpHealthRun).where(SanpHealthRun.completed_at < cutoff))

            if run_statuses and all(status is SanpImportStatus.FAILED for status in run_statuses):
                status = SanpImportStatus.FAILED
            elif any(status is not SanpImportStatus.COMPLETE for status in run_statuses):
                status = SanpImportStatus.PARTIAL
            else:
                status = SanpImportStatus.COMPLETE

            await _set_sync_status(
                db,
                attempted_at=attempted_at,
                succeeded_at=attempted_at if status is not SanpImportStatus.FAILED else None,
                error="; ".join(errors) or None,
                imported_run_count=imported_run_count,
            )
    except SanpHealthSyncInProgressError:
        raise
    except Exception:
        await _record_failed_sync_status(
            db,
            attempted_at=attempted_at,
            error="SANP Health synchronization failed",
        )
        raise

    return SanpHealthSyncResult(
        status=status,
        imported_run_count=imported_run_count,
        errors=errors,
    )


async def list_reports(db: AsyncSession) -> list[SanpHealthRun]:
    reports = await db.scalars(select(SanpHealthRun).order_by(SanpHealthRun.completed_at.desc()))
    return list(reports)


async def get_report_detail(
    db: AsyncSession, run_id: uuid.UUID
) -> SanpHealthReportDetailOut | None:
    run = await db.scalar(
        select(SanpHealthRun)
        .where(SanpHealthRun.id == run_id)
        .options(selectinload(SanpHealthRun.results).selectinload(SanpHealthResult.changes))
    )
    if run is None:
        return None

    results_by_spec: dict[str, dict[SanpEnvironment, SanpHealthResult]] = {}
    for result in run.results:
        results_by_spec.setdefault(result.spec_name, {})[result.environment] = result

    sanp_version = None
    for result in run.results:
        if result.environment is SanpEnvironment.SANP:
            match = SANP_VERSION_RE.search(result.source_branch)
            if match:
                sanp_version = match.group(1)
                break

    rows: list[SanpHealthMatrixRowOut] = []
    for spec_name in sorted(results_by_spec):
        environment_results = results_by_spec[spec_name]
        preferred = next(
            environment_results[environment]
            for environment in (
                SanpEnvironment.PRODUZIONE,
                SanpEnvironment.COLLAUDO,
                SanpEnvironment.SANP,
            )
            if environment in environment_results
        )

        def cell(
            environment: SanpEnvironment,
            environment_results: dict[SanpEnvironment, SanpHealthResult] = environment_results,
        ) -> SanpHealthMatrixCellOut:
            result = environment_results.get(environment)
            if result is None:
                return SanpHealthMatrixCellOut.not_configured(environment)
            return SanpHealthMatrixCellOut.model_validate(result)

        rows.append(
            SanpHealthMatrixRowOut(
                spec_name=spec_name,
                display_name=preferred.display_name,
                description=preferred.description,
                sanp=cell(SanpEnvironment.SANP),
                collaudo=cell(SanpEnvironment.COLLAUDO),
                produzione=cell(SanpEnvironment.PRODUZIONE),
            )
        )

    return SanpHealthReportDetailOut(
        report=SanpHealthRunOut.model_validate(run),
        sanp_version=sanp_version,
        items=rows,
    )


async def get_latest_report(db: AsyncSession) -> SanpHealthLatestResponse:
    latest_complete_id = await db.scalar(
        select(SanpHealthRun.id)
        .where(SanpHealthRun.import_status == SanpImportStatus.COMPLETE)
        .order_by(SanpHealthRun.completed_at.desc())
        .limit(1)
    )
    report = (
        await get_report_detail(db, latest_complete_id) if latest_complete_id is not None else None
    )
    sync_status = await get_sync_status(db)
    newest = await db.scalar(
        select(SanpHealthRun).order_by(SanpHealthRun.completed_at.desc()).limit(1)
    )

    sync_failed = bool(
        sync_status is not None
        and sync_status.last_error
        and (
            sync_status.last_success_at is None
            or sync_status.last_attempt_at > sync_status.last_success_at
        )
    )
    stale_reason = sync_status.last_error if sync_failed and sync_status is not None else None
    if (
        stale_reason is None
        and report is not None
        and newest is not None
        and newest.completed_at > report.report.completed_at
        and newest.completed_at
        >= (sync_status.last_attempt_at if sync_status is not None else _utcnow())
        - timedelta(hours=ARTIFACT_RETRY_HOURS)
        and newest.import_status is not SanpImportStatus.COMPLETE
    ):
        stale_reason = newest.error_message or "Latest report is incomplete"

    return SanpHealthLatestResponse(
        report=report,
        is_stale=stale_reason is not None,
        stale_reason=stale_reason,
    )


async def get_sync_status(db: AsyncSession) -> SanpHealthSyncStatus | None:
    return await db.get(SanpHealthSyncStatus, 1)

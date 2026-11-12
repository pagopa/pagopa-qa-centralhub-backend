from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.test_metrics import TestExecution, TestRun, TestSuite
from app.schemas.quality_gate import (
    NrtLaunchOptionsOut,
    NrtRunCreate,
    NrtSuiteOut,
    QualityGateRunCreate,
    QualityGateRunOut,
    QualityGateRunStatus,
    QualityGateSuitesOut,
)
from app.schemas.test_metrics import NrtListObject, TestRunOut, TestSuiteOut
from app.services.github import GitHubClient

logger = structlog.get_logger(__name__)

CALLER_ID = "pagopa-qa-centralhub"
AUDIT_ACTION = "quality_gate.run.dispatch"
RECENT_RUNS_WINDOW = timedelta(hours=1)
MAX_LISTED_RUNS = 20
DELETE_AUDIT_ACTION = "quality_gate.run.delete"


@dataclass(frozen=True)
class OriginConfig:
    """How the runs of one origin are dispatched and recognised on GitHub."""

    name: str
    # Name of the ``settings`` attribute holding the workflow file of this origin.
    workflow_setting: str
    # Prefix of the GitHub run name, in front of the correlation id.
    run_name_prefix: str
    # Run names of the runs launched from the Hub for this origin (external runs never match).
    title_regex: re.Pattern[str]


_HEX32 = "[0-9a-f]{32}"
ORIGINS: dict[str, OriginConfig] = {
    "QG": OriginConfig("QG", "tas_workflow_file", "tas-", re.compile(rf"^tas-qg-{_HEX32}$")),
    "TAS": OriginConfig("TAS", "tas_workflow_file", "tas-", re.compile(rf"^tas-{_HEX32}$")),
    "NRT": OriginConfig("NRT", "nrt_workflow_file", "nrt-", re.compile(rf"^nrt-{_HEX32}$")),
}


def _workflow_file(origin: str) -> str:
    return getattr(settings, ORIGINS[origin].workflow_setting)


class QualityGateConfigError(Exception):
    """The launch feature is not configured on this deployment."""


class QualityGateRunNotFoundError(Exception):
    """No test run exists with the given id."""


# Origins with their own pages: every other run is an NRT run (the ``/nrts`` list).
DEDICATED_ORIGINS = ("TAS", "QG")
NRT_ORIGIN = "NRT"


def origin_matches(run_origin: str | None, origin: str) -> bool:
    """Whether a run of ``run_origin`` belongs to the report asked for: ``QG``, ``TAS``, or
    ``NRT`` (any origin other than TAS and QG, as the NRT list shows them)."""
    if origin == NRT_ORIGIN:
        return run_origin not in DEDICATED_ORIGINS
    return run_origin == origin


class NotAQualityGateRunError(Exception):
    """The test run exists but was not produced by a Quality Gate (or by the origin asked for)."""


class QualityGateSuiteNotAllowedError(Exception):
    """The requested suite is not in the configured allowlist."""


class QualityGateSuiteTypeError(Exception):
    """The suite has no tests of the requested type in the TAS repository."""

    def __init__(self, suite: str, test_type: str, available: list[str]) -> None:
        super().__init__(suite)
        self.suite = suite
        self.test_type = test_type
        self.available = available


class NrtSuiteSelectionError(Exception):
    """The NRT selection names unknown suites, or no selected suite supports the environment."""

    def __init__(self, unknown: list[str], target_env: str) -> None:
        super().__init__(", ".join(unknown))
        self.unknown = unknown
        self.target_env = target_env


TEST_TYPES = ("integration", "e2e", "api")
SUITE_CATALOG_TTL_SECONDS = 60.0
_suite_catalog_cache: tuple[float, dict[str, list[str]]] | None = None


async def get_suite_catalog() -> dict[str, list[str]] | None:
    """Map each suite to the test types it has under `src/<type>/<suite>` in the TAS repository.

    Returns None when the repository cannot be read (missing permission, GitHub outage): callers
    must then skip the validation rather than block launches. Successful lookups are cached
    briefly, as the launch form and the launch itself both need them.
    """
    global _suite_catalog_cache  # noqa: PLW0603
    now = time.monotonic()
    if _suite_catalog_cache and now - _suite_catalog_cache[0] < SUITE_CATALOG_TTL_SECONDS:
        return _suite_catalog_cache[1]

    client = GitHubClient(token=settings.tas_github_token, repo=settings.tas_repo)
    catalog: dict[str, list[str]] = {}
    try:
        for test_type in TEST_TYPES:
            try:
                entries = await client.list_directory(f"src/{test_type}", ref=settings.tas_ref)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == httpx.codes.NOT_FOUND:
                    continue  # that test type has no folder at all
                raise
            for entry in entries:
                if entry.get("type") == "dir" and entry.get("name"):
                    catalog.setdefault(str(entry["name"]), []).append(test_type)
    except httpx.HTTPError:
        logger.warning("quality_gate suite catalog unavailable, skipping suite validation")
        return None

    _suite_catalog_cache = (now, catalog)
    return catalog


NRT_REGISTRY_PATH = ".github/nrt-suites.json"
_nrt_registry_cache: tuple[float, NrtLaunchOptionsOut] | None = None


async def get_nrt_launch_options() -> NrtLaunchOptionsOut:
    """Suites of the NRT registry (``.github/nrt-suites.json`` in the TAS repository).

    Not available (empty) when the repository cannot be read: callers must then skip the
    validation, the workflow stays the final check. Successful reads are cached briefly.
    """
    global _nrt_registry_cache  # noqa: PLW0603
    now = time.monotonic()
    if _nrt_registry_cache and now - _nrt_registry_cache[0] < SUITE_CATALOG_TTL_SECONDS:
        return _nrt_registry_cache[1]

    client = GitHubClient(token=settings.tas_github_token, repo=settings.tas_repo)
    try:
        raw = await client.get_file_content(NRT_REGISTRY_PATH, ref=settings.tas_ref)
        entries = json.loads(raw)["suites"]
        options = NrtLaunchOptionsOut(
            available=True,
            suites=[
                NrtSuiteOut(
                    id=str(e["id"]),
                    label=str(e.get("label") or e["id"]),
                    envs=[str(env) for env in e.get("envs", [])],
                )
                for e in entries
            ],
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        logger.warning("nrt suite registry unavailable, skipping suite validation")
        return NrtLaunchOptionsOut(available=False, suites=[])

    _nrt_registry_cache = (now, options)
    return options


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


def _dispatch_audit_action(origin: str) -> str:
    return AUDIT_ACTION if origin == "QG" else f"{origin.lower()}.run.dispatch"


async def launch_quality_gate_run(
    db: AsyncSession, body: QualityGateRunCreate, origin: str = "QG"
) -> QualityGateRunOut:
    """Dispatch the TAS workflow for a Quality Gate (default) or a TAS run.

    The two differ only in the ``isQualityGateRun`` flag (which sets the run origin) and in the
    correlation id: ``qg-<hex>`` for the gate, a bare ``<hex>`` for TAS (run name ``tas-<hex>``).
    """
    if not settings.tas_github_token:
        raise QualityGateConfigError("TAS_GITHUB_TOKEN is not configured")

    allowed = _allowed_suites()
    if allowed and body.test_suite not in allowed:
        raise QualityGateSuiteNotAllowedError(body.test_suite)

    # A suite/type pair that does not exist would only fail later inside the workflow, after
    # wasting a run: reject it here. When the repository cannot be read the check is skipped.
    catalog = await get_suite_catalog()
    if catalog is not None and body.test_type not in catalog.get(body.test_suite, []):
        raise QualityGateSuiteTypeError(
            body.test_suite, body.test_type, catalog.get(body.test_suite, [])
        )

    # The workflow run-name is "tas-<correlation_id>": "tas-qg-<hex>" or "tas-<hex>".
    is_gate = origin == "QG"
    correlation_id = f"qg-{uuid.uuid4().hex}" if is_gate else uuid.uuid4().hex
    inputs = {
        "test_type": body.test_type,
        "test_suite": body.test_suite,
        "environment": body.environment,
        "caller_id": CALLER_ID,
        "correlation_id": correlation_id,
        "tags": body.tags,
        "allure": "true" if body.allure else "false",
        "isQualityGateRun": "true" if is_gate else "false",
    }
    detail = {
        "origin": origin,
        "inputs": inputs,
        "repo": settings.tas_repo,
        "ref": settings.tas_ref,
    }

    client = GitHubClient(token=settings.tas_github_token, repo=settings.tas_repo)
    try:
        await client.dispatch_workflow(
            settings.tas_workflow_file, ref=settings.tas_ref, inputs=inputs
        )
    except Exception as exc:
        failure = {**detail, "outcome": "failed", "error": type(exc).__name__}
        await _audit(db, correlation_id, failure, action=_dispatch_audit_action(origin))
        raise
    await _audit(
        db,
        correlation_id,
        {**detail, "outcome": "dispatched"},
        action=_dispatch_audit_action(origin),
    )
    logger.info(
        "run dispatched", origin=origin, correlation_id=correlation_id, suite=body.test_suite
    )

    return QualityGateRunOut(
        correlation_id=correlation_id,
        run_name=f"tas-{correlation_id}",
        actions_url=(
            f"https://github.com/{settings.tas_repo}/actions/workflows/"
            f"{settings.tas_workflow_file}"
        ),
    )


async def launch_nrt_run(db: AsyncSession, body: NrtRunCreate) -> QualityGateRunOut:
    """Dispatch the NRT workflow for every suite of the environment or for a selection."""
    if not settings.tas_github_token:
        raise QualityGateConfigError("TAS_GITHUB_TOKEN is not configured")

    options = await get_nrt_launch_options()
    if options.available:
        known = {s.id: s for s in options.suites}
        selected = list(known) if body.suites == "all" else body.suites
        unknown = [s for s in selected if s not in known]
        supported = [s for s in selected if s in known and body.target_env in known[s].envs]
        if unknown or not supported:
            raise NrtSuiteSelectionError(unknown, body.target_env)

    correlation_id = uuid.uuid4().hex
    inputs = {
        "suites": "all" if body.suites == "all" else ",".join(body.suites),
        "target_env": body.target_env,
        "correlation_id": correlation_id,
    }
    detail = {
        "origin": "NRT",
        "inputs": inputs,
        "repo": settings.tas_repo,
        "ref": settings.tas_ref,
    }
    action = _dispatch_audit_action("NRT")

    client = GitHubClient(token=settings.tas_github_token, repo=settings.tas_repo)
    try:
        await client.dispatch_workflow(
            settings.nrt_workflow_file, ref=settings.tas_ref, inputs=inputs
        )
    except Exception as exc:
        failure = {**detail, "outcome": "failed", "error": type(exc).__name__}
        await _audit(db, correlation_id, failure, action=action)
        raise
    await _audit(db, correlation_id, {**detail, "outcome": "dispatched"}, action=action)
    logger.info("run dispatched", origin="NRT", correlation_id=correlation_id)

    return QualityGateRunOut(
        correlation_id=correlation_id,
        run_name=f"nrt-{correlation_id}",
        actions_url=(
            f"https://github.com/{settings.tas_repo}/actions/workflows/"
            f"{settings.nrt_workflow_file}"
        ),
    )


def _map_state(github_status: str | None) -> str:
    if github_status == "completed":
        return "completed"
    if github_status == "in_progress":
        return "in_progress"
    # queued, waiting, requested, pending: the run exists but has not started yet.
    return "queued"


def _to_status(correlation_id: str, run: dict | None, prefix: str) -> QualityGateRunStatus:
    run_name = f"{prefix}{correlation_id}"
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


async def get_quality_gate_run_status(
    correlation_id: str, origin: str = "QG"
) -> QualityGateRunStatus:
    """Live state of a run launched from the Hub for ``origin`` (QG, TAS or NRT)."""
    if not settings.tas_github_token:
        raise QualityGateConfigError("TAS_GITHUB_TOKEN is not configured")

    config = ORIGINS[origin]
    client = GitHubClient(token=settings.tas_github_token, repo=settings.tas_repo)
    run = await client.find_workflow_run_by_name(
        _workflow_file(origin), f"{config.run_name_prefix}{correlation_id}"
    )
    return _to_status(correlation_id, run, config.run_name_prefix)


def _is_recent(run: dict, now: datetime) -> bool:
    created = run.get("created_at")
    if not created:
        return False
    try:
        created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return False
    return now - created_at <= RECENT_RUNS_WINDOW


async def list_quality_gate_runs(origin: str = "QG") -> list[QualityGateRunStatus]:
    """Runs of ``origin`` launched from the Hub that are active or started recently.

    GitHub is the source of truth: nothing is stored on the Hub. Runs are recognised by
    their run name (``tas-qg-<hex>``, ``tas-<hex>`` or ``nrt-<hex>``); active runs are
    always listed, finished ones only within ``RECENT_RUNS_WINDOW`` so a run that failed
    before writing any report stays visible to the operator for a while.
    """
    if not settings.tas_github_token:
        raise QualityGateConfigError("TAS_GITHUB_TOKEN is not configured")

    client = GitHubClient(token=settings.tas_github_token, repo=settings.tas_repo)
    config = ORIGINS[origin]
    runs = await client.list_dispatched_runs(_workflow_file(origin))
    now = datetime.now(UTC)
    selected = [
        run
        for run in runs
        if config.title_regex.match(str(run.get("display_title", "")))
        and (run.get("status") != "completed" or _is_recent(run, now))
    ]
    selected.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    prefix = config.run_name_prefix
    return [
        _to_status(str(run["display_title"])[len(prefix) :], run, prefix)
        for run in selected[:MAX_LISTED_RUNS]
    ]


async def delete_quality_gate_run(
    db: AsyncSession, run_id: uuid.UUID, origin: str = "QG"
) -> None:
    """Delete a test run (its scenarios are removed by the FK cascade).

    Only runs of ``origin`` can be deleted (see :func:`origin_matches`), so each page can
    only delete its own runs. The published Allure report is not removed.
    """
    run = (await db.execute(select(TestRun).where(TestRun.id == run_id))).scalar_one_or_none()
    if run is None:
        raise QualityGateRunNotFoundError(str(run_id))
    if not origin_matches(run.test_origin, origin):
        raise NotAQualityGateRunError(str(run_id))

    detail = {
        "origin": origin,
        "suite_id": str(run.suite_id),
        "timestamp_start": str(run.timestamp_start),
        "allure_url": run.allure_url,
        "revision_result": run.revision_result.value if run.revision_result else None,
    }
    await db.execute(delete(TestRun).where(TestRun.id == run_id))
    await db.commit()
    await _audit(db, str(run_id), detail, action=DELETE_AUDIT_ACTION)
    logger.info("quality_gate run deleted", run_id=str(run_id))


async def list_quality_gate_run_scenarios(
    db: AsyncSession, run_id: uuid.UUID, origin: str = "QG"
) -> list[TestExecution]:
    """All scenarios of a run (passed ones included), for the run report.

    The report is shared by Quality Gate, TAS and NRT runs: ``origin`` is the one accepted
    (see :func:`origin_matches`).
    """
    run = (await db.execute(select(TestRun).where(TestRun.id == run_id))).scalar_one_or_none()
    if run is None:
        raise QualityGateRunNotFoundError(str(run_id))
    if not origin_matches(run.test_origin, origin):
        raise NotAQualityGateRunError(str(run_id))
    result = await db.execute(
        select(TestExecution)
        # The raw Allure JSONB is large and the report never reads it: do not fetch it.
        .options(defer(TestExecution.allure_report))
        .where(TestExecution.run_id == run_id)
        .order_by(TestExecution.scenario_name)
    )
    return list(result.scalars().all())


async def get_quality_gate_run(
    db: AsyncSession, run_id: uuid.UUID, origin: str = "QG"
) -> NrtListObject:
    """A single run of ``origin`` (Quality Gate by default, TAS or NRT) with its suite, in the
    same shape as the runs list.

    It backs the shareable report page, which opens from a link and cannot rely on the list.
    """
    row = (
        await db.execute(
            select(TestRun, TestSuite)
            .join(TestSuite, TestRun.suite_id == TestSuite.id)
            .where(TestRun.id == run_id)
        )
    ).one_or_none()
    if row is None:
        raise QualityGateRunNotFoundError(str(run_id))
    run, suite = row
    if not origin_matches(run.test_origin, origin):
        raise NotAQualityGateRunError(str(run_id))
    return NrtListObject(
        test_run=TestRunOut.model_validate(run),
        test_suite=TestSuiteOut.model_validate(suite),
    )


async def list_quality_gate_suites() -> QualityGateSuitesOut:
    """Launchable suites with their test types, for the launch form."""
    catalog = await get_suite_catalog()
    if catalog is None:
        return QualityGateSuitesOut(available=False, suites={})
    allowed = _allowed_suites()
    suites = {
        name: sorted(types, key=TEST_TYPES.index)
        for name, types in sorted(catalog.items())
        if not allowed or name in allowed
    }
    return QualityGateSuitesOut(available=True, suites=suites)

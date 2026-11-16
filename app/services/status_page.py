"""Read model for the Release Status feature: status grid, summary, detail and test linkage."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, cast

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.status_page import SpApplication, SpDeployEvent, SpProduct, SpVersionState
from app.models.test_metrics import EnvEnum, TestRun, TestSuite
from app.schemas.status_page import (
    ApplicationDetailOut,
    ApplicationRowOut,
    AreaGroupOut,
    CellOut,
    DeployEventOut,
    ProductGroupOut,
    RunRefOut,
    StatusOut,
    SummaryOut,
    SyncStatusOut,
)
from app.services.status_page_sync import get_sync_status
from app.services.status_page_versions import Drift, compare_versions, drift, normalize_version

logger = structlog.get_logger(__name__)

ENV_TARGETS = ("DEV", "UAT", "PROD")
ALL_TARGETS = ("REPO", *ENV_TARGETS)
KIND_ORDER = ("NRT", "QG", "TAS")
HUB_PATHS = {"QG": "/quality-gate/report", "TAS": "/tas-nrts/report", "NRT": "/nrts/report"}
AREA_ORDER = {"CORE": 0, "TOUCHPOINT": 1}
MAX_RUNS_PER_VERSION = 20

RunIndex = dict[tuple[str, str, str], list[Any]]


def run_kind(test_origin: str | None) -> str:
    """Map a run's ``test_origin`` to NRT, QG or TAS."""
    if test_origin in ("QG", "TAS"):
        return test_origin
    return "NRT"


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def run_outcome(run: TestRun) -> tuple[str, bool]:
    """Return ``(outcome, needs_review)`` for a run."""
    r: Any = run  # the legacy model uses untyped Column attributes
    return _outcome(r)


def _outcome(run: Any) -> tuple[str, bool]:
    kind = run_kind(run.test_origin)
    result = _enum_value(run.revision_result)
    if run.revisioned and result == "OK":
        return "PASSED", False
    if run.revisioned and result == "KO":
        return "FAILED", False
    needs_review = kind != "TAS"
    if run.failed_scenario is None and run.broken_scenario is None:
        return "NEUTRAL", needs_review
    bad = (run.failed_scenario or 0) + (run.broken_scenario or 0)
    return ("PASSED" if bad == 0 else "FAILED"), needs_review


def _failed_count(run: Any) -> int | None:
    """Failed plus broken scenarios, as counted by ``_outcome``; None when neither is set."""
    if run.failed_scenario is None and run.broken_scenario is None:
        return None
    return int((run.failed_scenario or 0) + (run.broken_scenario or 0))


def _run_ref(source: Any) -> RunRefOut:
    run: Any = source
    kind = run_kind(run.test_origin)
    outcome, needs_review = _outcome(run)
    trigger = _enum_value(run.trigger_type)
    return RunRefOut(
        run_id=run.id,
        kind=cast("Any", kind),
        outcome=cast("Any", outcome),
        needs_review=needs_review,
        trigger_type=str(trigger) if trigger is not None else None,
        timestamp_start=run.timestamp_start,
        passed=run.passed_scenario,
        failed=_failed_count(run),
        allure_url=run.allure_url,
        gha_run_url=run.gha_run_url,
        hub_url=f"{HUB_PATHS[kind]}/{run.id}",
    )


def _sorted_newest(runs: list[Any]) -> list[Any]:
    def key(run: Any) -> tuple[bool, float]:
        ts: Any = run.timestamp_start
        return (ts is not None, ts.timestamp() if ts is not None else 0.0)

    return sorted(runs, key=key, reverse=True)


def _latest_per_kind(runs: list[Any]) -> list[RunRefOut]:
    latest: dict[str, Any] = {}
    for run in _sorted_newest(runs):
        origin: Any = run.test_origin
        latest.setdefault(run_kind(origin), run)
    return [_run_ref(latest[k]) for k in KIND_ORDER if k in latest]


RUN_COLUMNS = (
    TestRun.id,
    TestRun.suite_id,
    TestRun.env,
    TestRun.test_origin,
    TestRun.revisioned,
    TestRun.revision_result,
    TestRun.passed_scenario,
    TestRun.failed_scenario,
    TestRun.broken_scenario,
    TestRun.trigger_type,
    TestRun.timestamp_start,
    TestRun.allure_url,
    TestRun.gha_run_url,
)


async def _fetch_runs(db: AsyncSession, needed: dict[str, set[str]]) -> RunIndex:
    """Load the runs of the given test objects, restricted to the needed (normalized) versions.

    ``needed`` maps a ``test_object`` to the normalized versions whose runs are shown.
    Only the columns used by ``_run_ref`` and ``_outcome`` are selected.
    """
    index: RunIndex = defaultdict(list)
    needed = {obj: versions for obj, versions in needed.items() if versions}
    if not needed:
        return index
    suites = await db.execute(
        select(TestSuite.id, TestSuite.test_object, TestSuite.test_object_version).where(
            TestSuite.test_object.in_(needed)
        )
    )
    suite_keys: dict[Any, tuple[str, str]] = {}
    for suite_id, test_object, version in suites.all():
        normalized = normalize_version(version)
        if normalized in needed[test_object]:
            suite_keys[suite_id] = (test_object, normalized)
    if not suite_keys:
        return index
    result = await db.execute(
        select(*RUN_COLUMNS).where(
            TestRun.suite_id.in_(suite_keys),
            TestRun.env.in_([EnvEnum.DEV, EnvEnum.UAT, EnvEnum.PROD]),
        )
    )
    for run in result.all():
        test_object, version = suite_keys[run.suite_id]
        index[(test_object, version, _enum_value(run.env))].append(run)
    return index


def _needed_versions(
    pairs: Sequence[tuple[SpApplication, str | None]],
) -> dict[str, set[str]]:
    """Group the normalized versions whose runs are needed by ``test_object``."""
    needed: dict[str, set[str]] = defaultdict(set)
    for app, version in pairs:
        if app.test_object is not None and version:
            needed[app.test_object].add(normalize_version(version))
    return needed


def _linked(runs: RunIndex, app: SpApplication, env: str, version: str | None) -> list[Any]:
    if app.test_object is None or not version:
        return []
    return runs.get((app.test_object, normalize_version(version), env), [])


def _build_row(
    app: SpApplication,
    states: dict[str, SpVersionState],
    runs: RunIndex,
) -> ApplicationRowOut:
    repo_state = states.get("REPO")
    repo_ok = repo_state is not None and repo_state.read_status == "OK"
    repo_version = repo_state.version if repo_ok and repo_state else None

    def cell(target: str) -> CellOut | None:
        state = states.get(target)
        if state is None:
            return None
        out = CellOut(
            version=state.version,
            read_status=state.read_status,
            error_detail=state.error_detail,
            checked_at=state.checked_at,
            last_ok_at=state.last_ok_at,
            version_since=state.version_since,
        )
        if target != "REPO":
            readable = state.read_status == "OK"
            out.drift = drift(state.version, repo_version) if readable else Drift.UNKNOWN
            out.tests = _latest_per_kind(_linked(runs, app, target, state.version))
        return out

    repo_cell = cell("REPO")
    envs = {t: cell(t) for t in ENV_TARGETS}

    flags: list[str] = []
    all_cells = [repo_cell, *envs.values()]
    all_ok = all(c is not None and c.read_status == "OK" for c in all_cells)
    prod = envs["PROD"]
    if prod is not None and prod.drift == Drift.EQUAL and all_ok:
        flags.append("ALIGNED")
    if prod is not None and prod.drift in (Drift.BEHIND, Drift.AHEAD):
        flags.append("DRIFT")
    if any(c is not None and c.read_status != "OK" for c in all_cells):
        flags.append("UNREACHABLE")
    if app.test_object is not None and any(
        envs[t] is not None
        and envs[t].version  # type: ignore[union-attr]
        and not envs[t].tests  # type: ignore[union-attr]
        for t in ("UAT", "PROD")
    ):
        flags.append("UNTESTED")

    return ApplicationRowOut(
        id=app.id,
        name=app.name,
        product_id=app.product_id,
        product_key=app.product_key,
        repository=app.repository,
        pipeline_url=app.pipeline_url,
        runtime=app.runtime,
        ci=app.ci,
        kind=app.kind,
        test_object=app.test_object,
        ea_node_id=app.ea_node_id,
        repo=repo_cell,
        envs=cast("Any", envs),
        flags=cast("Any", flags),
    )


async def _load_all(
    db: AsyncSession,
) -> tuple[
    list[tuple[SpApplication, SpProduct]],
    dict[uuid.UUID, dict[str, SpVersionState]],
    RunIndex,
]:
    pairs = [
        (a, p)
        for a, p in (await db.execute(select(SpApplication, SpProduct).join(SpProduct))).all()
    ]
    states: dict[uuid.UUID, dict[str, SpVersionState]] = defaultdict(dict)
    for state in (await db.execute(select(SpVersionState))).scalars():
        states[state.application_id][state.target] = state
    deployed = [
        (app, state.version)
        for app, _ in pairs
        for target, state in states.get(app.id, {}).items()
        if target in ENV_TARGETS
    ]
    runs = await _fetch_runs(db, _needed_versions(deployed))
    return pairs, states, runs


async def get_status(db: AsyncSession) -> StatusOut:
    try:
        pairs, states, runs = await _load_all(db)
        sync = await get_sync_status(db)
        products: dict[uuid.UUID, tuple[SpProduct, list[ApplicationRowOut]]] = {}
        for app, product in pairs:
            products.setdefault(product.id, (product, []))[1].append(
                _build_row(app, states.get(app.id, {}), runs)
            )
        by_area: dict[str, list[ProductGroupOut]] = defaultdict(list)
        for product, rows in products.values():
            rows.sort(key=lambda r: r.name)
            by_area[product.area].append(
                ProductGroupOut(id=product.id, name=product.name, applications=rows)
            )
        areas = [
            AreaGroupOut(area=area, products=sorted(groups, key=lambda g: g.name))
            for area, groups in sorted(by_area.items(), key=lambda kv: AREA_ORDER.get(kv[0], 99))
        ]
        if not pairs:
            logger.info("get_status query succeeded with 0 results")
        return StatusOut(
            sync=SyncStatusOut.model_validate(sync) if sync is not None else None, areas=areas
        )
    except Exception:
        logger.exception("get_status failed")
        raise


async def get_summary(db: AsyncSession) -> SummaryOut:
    try:
        pairs, states, runs = await _load_all(db)
        rows = [_build_row(a, states.get(a.id, {}), runs) for a, _ in pairs]
        sync = await get_sync_status(db)
        return SummaryOut(
            total=len(rows),
            aligned=sum("ALIGNED" in r.flags for r in rows),
            drift=sum("DRIFT" in r.flags for r in rows),
            unreachable=sum("UNREACHABLE" in r.flags for r in rows),
            untested=sum("UNTESTED" in r.flags for r in rows),
            last_success_at=sync.last_success_at if sync is not None else None,
        )
    except Exception:
        logger.exception("get_summary failed")
        raise


def _direction(event: SpDeployEvent) -> str:
    if event.is_baseline or event.previous_version is None:
        return "baseline"
    result = compare_versions(event.version, event.previous_version)
    if result is None:
        return "unknown"
    return "upgrade" if result > 0 else "rollback" if result < 0 else "unknown"


async def get_application_detail(
    db: AsyncSession, application_id: uuid.UUID
) -> ApplicationDetailOut | None:
    try:
        found = (
            await db.execute(
                select(SpApplication, SpProduct)
                .join(SpProduct)
                .where(SpApplication.id == application_id)
            )
        ).first()
        if found is None:
            logger.info("get_application_detail query succeeded with 0 results")
            return None
        app, product = found
        states = {
            s.target: s
            for s in (
                await db.execute(
                    select(SpVersionState).where(SpVersionState.application_id == app.id)
                )
            ).scalars()
        }
        events = (
            (
                await db.execute(
                    select(SpDeployEvent)
                    .where(SpDeployEvent.application_id == app.id)
                    .order_by(SpDeployEvent.detected_at.desc())
                )
            )
            .scalars()
            .all()
        )
        versions: list[tuple[SpApplication, str | None]] = [
            (app, ev.version) for ev in events if ev.target in ENV_TARGETS
        ]
        versions += [(app, st.version) for t, st in states.items() if t in ENV_TARGETS]
        runs = await _fetch_runs(db, _needed_versions(versions))
        history: dict[str, list[DeployEventOut]] = {t: [] for t in ALL_TARGETS}
        for ev in events:
            linked: list[RunRefOut] = []
            if ev.target in ENV_TARGETS:
                linked = [
                    _run_ref(r)
                    for r in _sorted_newest(_linked(runs, app, ev.target, ev.version))[
                        :MAX_RUNS_PER_VERSION
                    ]
                ]
            history[ev.target].append(
                DeployEventOut(
                    version=ev.version,
                    previous_version=ev.previous_version,
                    detected_at=ev.detected_at,
                    previous_seen_at=ev.previous_seen_at,
                    is_baseline=ev.is_baseline,
                    direction=cast("Any", _direction(ev)),
                    runs=linked,
                )
            )
        return ApplicationDetailOut(
            application=_build_row(app, states, runs),
            product_name=product.name,
            history=cast("Any", history),
        )
    except Exception:
        logger.exception("get_application_detail failed")
        raise

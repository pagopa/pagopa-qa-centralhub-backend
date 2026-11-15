"""Flaky scenarios: score recompute, list/summary/history queries and flag management."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import (
    ColumnElement,
    Select,
    Subquery,
    and_,
    case,
    delete,
    false,
    func,
    literal,
    null,
    or_,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.flaky import FlakyRecomputeStatus, FlakyRegistry, FlakyScore
from app.models.test_metrics import (
    EnvEnum,
    ScenarioStatusEnum,
    TestExecution,
    TestRun,
    TestSuite,
    TriggerTypeEnum,
)
from app.schemas.common import PaginatedResponse
from app.schemas.flaky import (
    FlakyFlagFilter,
    FlakyFlagIn,
    FlakyHint,
    FlakyHistoryItemOut,
    FlakyScenarioRowOut,
    FlakySummaryOut,
)
from app.services.flaky_scoring import ExecutionPoint, ScoringConfig, compute_score, outcome_for

logger = structlog.get_logger(__name__)

RECOMPUTE_LOCK_ID = 670
ELIGIBLE_TRIGGERS = (TriggerTypeEnum.CRON, TriggerTypeEnum.CI_PIPELINE, TriggerTypeEnum.TAS)
# Quality Gate runs test release candidates, where a failure is usually a real regression. The
# ingestion sends them with trigger CI_PIPELINE, so they are excluded by origin.
EXCLUDED_ORIGINS = ("QG",)

ScenarioKey = tuple[str, str, str]  # test_object, test_type, scenario_name
RecomputeTrigger = Literal["SCHEDULE", "MANUAL"]


class FlakyRecomputeInProgressError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _eligible_executions(now: datetime) -> Subquery:
    """Eligible, non-skipped executions in the window, ranked newest first per scenario/env."""
    rank = (
        func.row_number()
        .over(
            partition_by=(
                TestSuite.test_object,
                TestSuite.test_type,
                TestExecution.scenario_name,
                TestRun.env,
            ),
            order_by=(TestRun.timestamp_start.desc(), TestRun.id.desc()),
        )
        .label("rank")
    )
    return (
        select(
            TestSuite.test_object,
            TestSuite.test_type,
            TestExecution.scenario_name,
            TestRun.env,
            TestRun.id.label("run_id"),
            TestRun.timestamp_start,
            TestRun.suite_id,
            TestRun.test_version,
            TestRun.test_origin,
            TestSuite.test_object_version,
            TestExecution.status,
            TestExecution.retries,
            TestExecution.error_message,
            TestExecution.allure_url,
            rank,
        )
        .join(TestRun, TestRun.id == TestExecution.run_id)
        .join(TestSuite, TestSuite.id == TestRun.suite_id)
        .where(
            TestRun.trigger_type.in_(ELIGIBLE_TRIGGERS),
            or_(TestRun.test_origin.is_(None), TestRun.test_origin.not_in(EXCLUDED_ORIGINS)),
            TestRun.env.is_not(None),
            TestRun.timestamp_start.is_not(None),
            TestRun.timestamp_start >= now - timedelta(days=settings.flaky_window_days),
            TestExecution.scenario_name.is_not(None),
            TestExecution.status.is_not(None),
            TestExecution.status != ScenarioStatusEnum.SKIPPED,
        )
        .subquery()
    )


async def load_execution_points(
    db: AsyncSession, now: datetime
) -> dict[tuple[ScenarioKey, EnvEnum], list[ExecutionPoint]]:
    """Up to `flaky_max_runs` eligible executions per scenario and environment, oldest first."""
    try:
        ranked = _eligible_executions(now)
        rows = (
            await db.execute(
                select(ranked)
                .where(ranked.c.rank <= settings.flaky_max_runs)
                .order_by(ranked.c.rank.desc())
            )
        ).all()
        if not rows:
            logger.info("load_execution_points query succeeded with 0 results")
        points: dict[tuple[ScenarioKey, EnvEnum], list[ExecutionPoint]] = defaultdict(list)
        for row in rows:
            outcome = outcome_for(row.status.value, row.retries)
            if outcome is None:
                continue
            key = ((row.test_object, row.test_type, row.scenario_name), row.env)
            points[key].append(
                ExecutionPoint(
                    run_id=row.run_id,
                    ts=row.timestamp_start,
                    outcome=outcome,
                    suite_id=row.suite_id,
                )
            )
        return dict(points)
    except Exception:
        logger.exception("load_execution_points failed")
        raise


def _score_row(
    key: ScenarioKey, env: EnvEnum, points: list[ExecutionPoint], now: datetime, cfg: ScoringConfig
) -> FlakyScore:
    result = compute_score(points, now, cfg)
    test_object, test_type, scenario_name = key
    return FlakyScore(
        test_object=test_object,
        test_type=test_type,
        scenario_name=scenario_name,
        env=env,
        score=result.score,
        level=result.level,
        state=result.state,
        flip_rate=result.flip_rate,
        retry_pass_rate=result.retry_pass_rate,
        runs_analyzed=result.runs_analyzed,
        last_failure_at=result.last_failure_at,
        recent_outcomes=[
            {
                "run_id": str(point.run_id),
                "ts": point.ts.isoformat(),
                "outcome": point.outcome.value,
            }
            for point in points
        ],
        last_suite_id=points[-1].suite_id if points else None,
        computed_at=now,
    )


async def _status_row(db: AsyncSession, attempted_at: datetime) -> FlakyRecomputeStatus:
    status = await db.get(FlakyRecomputeStatus, 1)
    if status is None:
        status = FlakyRecomputeStatus(id=1, last_attempt_at=attempted_at, scored_count=0)
        db.add(status)
    return status


async def _record_failure(
    db: AsyncSession,
    *,
    attempted_at: datetime,
    trigger: RecomputeTrigger,
    requested_by: str | None,
    error: str,
) -> None:
    try:
        status = await _status_row(db, attempted_at)
        status.last_attempt_at = attempted_at
        status.last_trigger = trigger
        status.last_requested_by = requested_by
        status.last_error = error
        await db.commit()
    except Exception:
        # Recording the failure must never hide the original error.
        logger.exception("flaky recompute failure status write failed")
        await db.rollback()


async def recompute_scores(
    db: AsyncSession,
    *,
    trigger: RecomputeTrigger,
    requested_by: str | None,
    now: datetime | None = None,
) -> FlakyRecomputeStatus:
    """Replace every flaky score in one transaction, guarded by an advisory lock."""
    attempted_at = now or _utcnow()
    try:
        # Start from a clean transaction so the lock and the replacement share it.
        await db.commit()
        locked = bool(await db.scalar(select(func.pg_try_advisory_xact_lock(RECOMPUTE_LOCK_ID))))
        if not locked:
            await db.rollback()
            raise FlakyRecomputeInProgressError("Flaky scores recompute is already running")

        cfg = ScoringConfig.from_settings()
        points = await load_execution_points(db, attempted_at)
        scores = [
            _score_row(key, env, items, attempted_at, cfg) for (key, env), items in points.items()
        ]

        await db.execute(delete(FlakyScore))
        db.add_all(scores)

        status = await _status_row(db, attempted_at)
        status.last_attempt_at = attempted_at
        status.last_success_at = attempted_at
        status.last_error = None
        status.last_trigger = trigger
        status.last_requested_by = requested_by
        status.scored_count = len(scores)
        await db.commit()
        await db.refresh(status)
        logger.info("flaky_recompute_ok", trigger=trigger, scored_count=len(scores))
        return status
    except FlakyRecomputeInProgressError:
        logger.info("flaky recompute skipped: already running", trigger=trigger)
        raise
    except Exception as exc:
        logger.exception("recompute_scores failed")
        await db.rollback()
        await _record_failure(
            db,
            attempted_at=attempted_at,
            trigger=trigger,
            requested_by=requested_by,
            error=str(exc),
        )
        raise


async def get_recompute_status(db: AsyncSession) -> FlakyRecomputeStatus | None:
    try:
        status = await db.get(FlakyRecomputeStatus, 1)
        if status is None:
            logger.info("get_recompute_status query succeeded with 0 results")
        return status
    except Exception:
        logger.exception("get_recompute_status failed")
        raise


# ── Queries and flags ────────────────────────────────────────────────────


def _scenario_rows(env: EnvEnum) -> Subquery:
    """Scores of `env` joined with the registry; flagged scenarios without a score are kept."""
    scores = select(FlakyScore).where(FlakyScore.env == env).subquery("scores")
    registry = FlakyRegistry.__table__
    is_flaky = func.coalesce(registry.c.is_flaky, false())
    state = func.coalesce(scores.c.state, "INSUFFICIENT_DATA")
    runs = func.coalesce(scores.c.runs_analyzed, 0)
    hint = case(
        (state == "BROKEN", literal("broken")),
        (and_(state == "OK", scores.c.level >= 4, ~is_flaky), literal("to_mark")),
        (
            and_(
                is_flaky, state == "OK", scores.c.level == 1, runs >= settings.flaky_unmark_min_runs
            ),
            literal("to_unmark"),
        ),
        else_=null(),
    )
    on_key = and_(
        scores.c.test_object == registry.c.test_object,
        scores.c.test_type == registry.c.test_type,
        scores.c.scenario_name == registry.c.scenario_name,
    )
    return (
        select(
            func.coalesce(scores.c.test_object, registry.c.test_object).label("test_object"),
            func.coalesce(scores.c.test_type, registry.c.test_type).label("test_type"),
            func.coalesce(scores.c.scenario_name, registry.c.scenario_name).label("scenario_name"),
            scores.c.score,
            scores.c.level,
            state.label("state"),
            scores.c.flip_rate,
            scores.c.retry_pass_rate,
            runs.label("runs_analyzed"),
            scores.c.last_failure_at,
            func.coalesce(scores.c.recent_outcomes, text("'[]'::jsonb")).label("recent_outcomes"),
            scores.c.last_suite_id,
            is_flaky.label("is_flaky"),
            registry.c.flagged_by,
            registry.c.flagged_at,
            hint.label("hint"),
            (scores.c.test_object.is_not(None)).label("has_score"),
        )
        .select_from(scores.join(registry, on_key, full=True))
        .where(or_(scores.c.test_object.is_not(None), registry.c.is_flaky.is_(True)))
        .subquery("flaky_rows")
    )


def _apply_filters(
    stmt: Select[Any],
    rows: Subquery,
    *,
    test_objects: list[str] | None,
    flag: FlakyFlagFilter,
    min_level: int | None,
    q: str | None,
) -> Select[Any]:
    if test_objects:
        stmt = stmt.where(rows.c.test_object.in_(test_objects))
    if flag == "flagged":
        stmt = stmt.where(rows.c.is_flaky.is_(True))
    elif flag == "unflagged":
        stmt = stmt.where(rows.c.is_flaky.is_(False))
    if min_level is not None:
        stmt = stmt.where(rows.c.level >= min_level)
    if q and q.strip():
        stmt = stmt.where(rows.c.scenario_name.ilike(f"%{q.strip()}%"))
    return stmt


async def list_scenarios(
    db: AsyncSession,
    *,
    env: EnvEnum,
    test_objects: list[str] | None,
    flag: FlakyFlagFilter,
    min_level: int | None,
    hint: FlakyHint | None,
    q: str | None,
    page: int,
    page_size: int,
    scenario_names: list[str] | None = None,
) -> PaginatedResponse[FlakyScenarioRowOut]:
    try:
        rows = _scenario_rows(env)
        stmt = _apply_filters(
            select(rows), rows, test_objects=test_objects, flag=flag, min_level=min_level, q=q
        )
        if hint is not None:
            stmt = stmt.where(rows.c.hint == hint)
        if scenario_names:
            stmt = stmt.where(rows.c.scenario_name.in_(scenario_names))
        total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        result = await db.execute(
            stmt.order_by(
                rows.c.level.desc().nulls_last(),
                rows.c.score.desc().nulls_last(),
                rows.c.scenario_name.asc(),
                rows.c.test_object.asc(),
                rows.c.test_type.asc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = [
            FlakyScenarioRowOut.model_validate({**row._mapping, "env": env.value})
            for row in result.all()
        ]
        if not items:
            logger.info("list_scenarios query succeeded with 0 results", env=env.value)
        return PaginatedResponse[FlakyScenarioRowOut](
            items=items, total=total, page=page, page_size=page_size
        )
    except Exception:
        logger.exception("list_scenarios failed")
        raise


async def get_summary(
    db: AsyncSession,
    *,
    env: EnvEnum,
    test_objects: list[str] | None,
    flag: FlakyFlagFilter,
    min_level: int | None,
    q: str | None,
) -> FlakySummaryOut:
    try:
        rows = _scenario_rows(env)

        def count_when(condition: ColumnElement[bool]) -> ColumnElement[int]:
            return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

        stmt = _apply_filters(
            select(
                count_when(rows.c.hint == "to_mark").label("to_mark"),
                count_when(rows.c.hint == "to_unmark").label("to_unmark"),
                count_when(rows.c.hint == "broken").label("broken"),
                count_when(rows.c.is_flaky.is_(True)).label("flagged"),
                count_when(rows.c.has_score.is_(True)).label("analyzed"),
            ),
            rows,
            test_objects=test_objects,
            flag=flag,
            min_level=min_level,
            q=q,
        )
        row = (await db.execute(stmt)).one()
        return FlakySummaryOut.model_validate(dict(row._mapping))
    except Exception:
        logger.exception("get_summary failed")
        raise


async def get_history(
    db: AsyncSession, *, env: EnvEnum, test_object: str, test_type: str, scenario_name: str
) -> list[FlakyHistoryItemOut]:
    """Executions used by the score of one scenario in `env`, newest first."""
    try:
        ranked = _eligible_executions(_utcnow())
        result = await db.execute(
            select(ranked)
            .where(
                ranked.c.rank <= settings.flaky_max_runs,
                ranked.c.env == env,
                ranked.c.test_object == test_object,
                ranked.c.test_type == test_type,
                ranked.c.scenario_name == scenario_name,
            )
            .order_by(ranked.c.rank.asc())
        )
        items = []
        for row in result.all():
            outcome = outcome_for(row.status.value, row.retries)
            if outcome is None:
                continue
            items.append(
                FlakyHistoryItemOut(
                    run_id=row.run_id,
                    ts=row.timestamp_start,
                    outcome=outcome.value,
                    status=row.status.value,
                    retries=row.retries,
                    error_message=row.error_message,
                    allure_url=row.allure_url,
                    test_version=row.test_version,
                    test_object_version=row.test_object_version,
                    test_origin=row.test_origin,
                )
            )
        if not items:
            logger.info("get_history query succeeded with 0 results", env=env.value)
        return items
    except Exception:
        logger.exception("get_history failed")
        raise


async def _audit_flag(db: AsyncSession, registry: FlakyRegistry, user: str | None) -> None:
    try:
        db.add(
            AuditLog(
                actor_id=None,
                action="flaky.mark" if registry.is_flaky else "flaky.unmark",
                resource_type="flaky_scenario",
                resource_id=str(registry.id),
                detail=json.dumps(
                    {
                        "test_object": registry.test_object,
                        "test_type": registry.test_type,
                        "scenario_name": registry.scenario_name,
                        "user": user,
                    }
                ),
            )
        )
        await db.commit()
    except Exception:
        # An audit failure must never hide the flag change itself.
        logger.exception("flaky audit log write failed", registry_id=str(registry.id))
        await db.rollback()


async def apply_flag(
    db: AsyncSession,
    *,
    key: ScenarioKey,
    is_flaky: bool,
    user: str | None,
    now: datetime | None = None,
) -> FlakyRegistry:
    """Mark or unmark a scenario; who and when are kept on the registry row and in the audit log."""
    try:
        test_object, test_type, scenario_name = key
        changed_at = now or _utcnow()
        registry = await db.scalar(
            select(FlakyRegistry).where(
                FlakyRegistry.test_object == test_object,
                FlakyRegistry.test_type == test_type,
                FlakyRegistry.scenario_name == scenario_name,
            )
        )
        if registry is None:
            logger.info("apply_flag query succeeded with 0 results; inserting scenario")
            registry = FlakyRegistry(
                test_object=test_object,
                test_type=test_type,
                scenario_name=scenario_name,
                is_flaky=is_flaky,
                inserted_at=changed_at,
            )
            db.add(registry)
        elif registry.is_flaky == is_flaky:
            # Report panels re-post every known flag: an unchanged flag keeps who set it, and when.
            return registry
        registry.is_flaky = is_flaky
        registry.updated_by = user
        registry.updated_at = changed_at
        if is_flaky:
            registry.flagged_by = user
            registry.flagged_at = changed_at
        await db.commit()
        await db.refresh(registry)
        await _audit_flag(db, registry, user)
        return registry
    except Exception:
        logger.exception("apply_flag failed")
        await db.rollback()
        raise


async def set_flag(
    db: AsyncSession, body: FlakyFlagIn, now: datetime | None = None
) -> FlakyRegistry:
    return await apply_flag(
        db,
        key=(body.test_object, body.test_type, body.scenario_name),
        is_flaky=body.is_flaky,
        user=body.user,
        now=now,
    )

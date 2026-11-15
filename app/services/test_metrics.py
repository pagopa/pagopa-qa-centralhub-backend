from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DatabaseError, DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

# to import models
from app.models.flaky import FlakyRegistry
from app.models.test_metrics import RevisionResultEnum, TestExecution, TestRun, TestSuite
from app.services import flaky as flaky_svc
from app.schemas.test_metrics import (
    FlakyScenarioIn,
    FlakyScenarioOut,
    NrtDetailsObject,
    PendingRunsOut,
    NrtListObject,
    TestExecutionOut,
    TestRunOut,
    TestSuiteOut,
    UpdateNrtsObject,
)

logger = structlog.get_logger(__name__)

TREND_WINDOW = 10


async def create_test_suite(db: AsyncSession, test_suite: TestSuite):
    try:
        db.add(test_suite)
        await db.commit()
        await db.refresh(test_suite)
        return test_suite
    except Exception:
        logger.exception("create_test_suite failed")
        raise
    

async def get_latest_test_suite_version(db: AsyncSession, test_object_in: str) -> TestSuite:
    try:
        result = await db.execute(
            select(TestSuite).filter_by(test_object=test_object_in).order_by(TestSuite.test_object_version.desc()).limit(1)
        )
        suite = result.scalar_one_or_none()
        if suite is None:
            logger.info("get_latest_test_suite_version query succeeded with 0 results", test_object=test_object_in)
        return suite
    except Exception:
        logger.exception("get_latest_test_suite_version failed")
        raise

async def create_test_run(db: AsyncSession, test_run: TestRun):
    try:
        db.add(test_run)
        await db.commit()
        await db.refresh(test_run)
        return test_run
    except Exception:
        logger.exception("create_test_run failed")
        raise

async def create_test_execution(db: AsyncSession, test_execution: list[TestExecution]):
    try:
        if not test_execution:
            logger.info("create_test_execution: no input records to insert")
        db.add_all(test_execution)
        await db.commit()
        for te in test_execution:
            await db.refresh(te)
        return test_execution
    except Exception:
        logger.exception("create_test_execution failed")
        raise

NRT_LIST_WINDOW_DAYS = 30


def _list_window_start() -> datetime:
    """Oldest end time of the runs shown in the NRT, TAS and Quality Gate lists."""
    return datetime.now(timezone.utc) - timedelta(days=NRT_LIST_WINDOW_DAYS)


async def get_nrts(db: AsyncSession, test_object: str | None, origin: str | None, page:int = 1, size:int = 20) -> list[NrtListObject]:
    try:
        stmt = (
            select(TestRun, TestSuite)
            .join(TestSuite, TestRun.suite_id == TestSuite.id)
            .where(TestRun.timestamp_end > _list_window_start())
            .order_by(TestRun.timestamp_end.desc())
            .offset((page - 1) * size)
            .limit(size)
        )

        count_stmt = (
                select(func.count()).select_from(TestRun).where(TestRun.timestamp_end > _list_window_start())
        )

        if test_object:
            stmt = stmt.where(TestSuite.test_object == test_object)
            count_stmt = count_stmt.join(TestSuite, TestRun.suite_id == TestSuite.id).where(TestSuite.test_object == test_object)

        if origin:
            count_stmt = count_stmt.where(TestRun.test_origin == origin)
            stmt = stmt.where(TestRun.test_origin == origin)
        else:
            count_stmt = count_stmt.where(TestRun.test_origin.not_in(("TAS", "QG")))
            stmt = stmt.where(TestRun.test_origin.not_in(("TAS", "QG")))

        total: int = await db.scalar(count_stmt) or 0
        if total == 0:
            logger.info("get_nrts count query succeeded with 0 matching records", test_object=test_object, origin=origin)
        result = await db.execute(stmt)
        result_unzip = result.all()
        if not result_unzip:
            logger.info("get_nrts query succeeded with 0 results", page=page, size=size, total=total)
        for run, suite in result_unzip:
            run.timestamp_start = datetime.fromtimestamp(run.timestamp_start.timestamp()).strftime("%Y-%m-%d %H:%M:%S")
            run.timestamp_end = datetime.fromtimestamp(run.timestamp_end.timestamp()).strftime("%Y-%m-%d %H:%M:%S")

        return [
            NrtListObject(
                test_run=TestRunOut.model_validate(run),
                test_suite=TestSuiteOut.model_validate(suite),
            )
            for run, suite in result_unzip
        ], total
    except Exception:
        logger.exception("get_nrts failed")
        raise

async def get_nrt_by_id(db: AsyncSession, id: str) -> NrtDetailsObject | None:
    try:
        stmt = (
                select(TestExecution)
                .join(TestRun, TestExecution.run_id == TestRun.id)
                .where(TestExecution.run_id == id)
                .where(TestExecution.status != "PASSED")
                .order_by(TestExecution.duration_ms.desc())
            )
        result = await db.execute(stmt)
        executions = result.scalars().all() if result is not None else None
        if not executions:
            logger.info("get_nrt_by_id query succeeded with 0 results", run_id=id)
        return executions
    except Exception:
        logger.exception("get_nrt_by_id failed")
        raise



async def update_nrt(db: AsyncSession, nrt: UpdateNrtsObject) -> UpdateNrtsObject | None:
    try:
        revision_date = datetime.now(timezone.utc)
        test_run_updates = []
        for run in nrt.test_runs or []:
            if run is None:
                continue
            run.revisioned = True
            run.revision_date = revision_date
            test_run_updates.append(
                run.model_dump(
                    include={"id", "revisioned", "revision_date", "revisioned_by", "revision_result", "product_issue","qa_issue","product_issue_status","qa_issue_status","note"},
                    exclude_unset=True,
                )
            )
            if "note" in test_run_updates[-1]:
                # An empty note clears it.
                test_run_updates[-1]["note"] = test_run_updates[-1]["note"] or None
        test_execution_fields = { "revision_note"}
        test_execution_updates = [
            values
            for scenario in nrt.scenarios or []
            if (values := scenario.model_dump(include={"id", *test_execution_fields}, exclude_unset=True))
            and len(values) > 1
        ]

        if not test_run_updates and not test_execution_updates:
            logger.info("update_nrt: no input records to update")
        if test_run_updates:
            await db.execute(update(TestRun), test_run_updates)
        if test_execution_updates:
            await db.execute(update(TestExecution), test_execution_updates)
        await db.commit()
        return nrt
    except Exception:
        logger.exception("update_nrt failed")
        raise


async def get_flaky_scenarios(db: AsyncSession, test_object: str | None) -> list[FlakyScenarioOut]:
    """Flagged scenarios from the flaky registry; version fields are left empty."""
    try:
        stmt = select(FlakyRegistry).where(FlakyRegistry.is_flaky.is_(True))
        if test_object:
            stmt = stmt.where(FlakyRegistry.test_object == test_object)
        result = await db.execute(
            stmt.order_by(FlakyRegistry.test_type, FlakyRegistry.scenario_name)
        )
        scenarios = [_registry_to_flaky_out(row) for row in result.scalars().all()]
        if not scenarios:
            logger.info("get_flaky_scenarios query succeeded with 0 results", test_object=test_object)
        return scenarios
    except Exception:
        logger.exception("get_flaky_scenarios failed")
        raise


def _registry_to_flaky_out(registry: FlakyRegistry) -> FlakyScenarioOut:
    return FlakyScenarioOut(
        id=registry.id,
        test_object=registry.test_object,
        test_type=registry.test_type,
        scenario_name=registry.scenario_name,
        inserted_at=registry.inserted_at,
        is_flaky=registry.is_flaky,
    )


async def save_or_update_flaky_scenarios(
    db: AsyncSession, flaky_scenarios: list[FlakyScenarioIn]
) -> list[FlakyScenarioOut] | None:
    """Write NRT/TAS/QG panel flags into the flaky registry (legacy table no longer written)."""
    try:
        if not flaky_scenarios:
            logger.info("save_or_update_flaky_scenarios: no input records to save")
            return None
        processed: list[FlakyScenarioOut] = []
        for scenario in flaky_scenarios:
            test_type = scenario.test_type
            if not test_type:
                suite = await db.get(TestSuite, scenario.suite_id)
                if suite is None:
                    logger.info(
                        "save_or_update_flaky_scenarios suite query succeeded with 0 results",
                        suite_id=str(scenario.suite_id),
                    )
                    raise LookupError(f"Unknown suite: {scenario.suite_id}")
                test_type = suite.test_type
            registry = await flaky_svc.apply_flag(
                db,
                key=(scenario.test_object, test_type, scenario.scenario_name),
                is_flaky=scenario.is_flaky,
                user=scenario.user,
            )
            processed.append(_registry_to_flaky_out(registry))
        return processed
    except Exception:
        logger.exception("save_or_update_flaky_scenarios failed")
        raise


async def list_suites_with_latest_run(
    db: AsyncSession,
    origin: str | None = None,
) -> list[tuple[TestSuite, TestRun | None, list[float]]]:
    try:
        stmt = (
            select(TestSuite.test_object)
            .join(TestRun, TestRun.suite_id == TestSuite.id)
            .distinct()
            .order_by(TestSuite.test_object)
        )

        if origin:
            stmt = stmt.where(TestRun.test_origin == origin)
        else:
            stmt = stmt.where(TestRun.test_origin.not_in(("TAS", "QG")))

        test_objects = list(await db.scalars(stmt))
        if not test_objects:
            logger.info("list_suites_with_latest_run query succeeded with 0 results")
        result: list[tuple[TestSuite, TestRun | None, list[float]]] = []
        for test_object in test_objects:
            stmt = (
                select(TestRun, TestSuite)
                .join(TestSuite, TestRun.suite_id == TestSuite.id)
                .where(TestSuite.test_object == test_object)
                .order_by(TestRun.timestamp_end.desc().nullslast())
                .limit(TREND_WINDOW)
            )

            if origin:
                stmt = stmt.where(TestRun.test_origin == origin)
            else:
                stmt = stmt.where(TestRun.test_origin.not_in(("TAS", "QG")))
            
            rows = (await db.execute(stmt)).all()
            if not rows:
                logger.info("list_suites_with_latest_run query succeeded with 0 results")
                continue
            latest, suite = rows[0]
            trend: list[float] = []
            for run, _ in reversed(rows):
                trend.append(round(run.passed_scenario / run.scenario_qty, 4) if run.scenario_qty and run.scenario_qty > 0 else 0.0)
            result.append((suite, latest, trend))
        return result
    except Exception:
        logger.exception("list_suites_with_latest_run failed")
        raise


async def get_pending_runs(db: AsyncSession) -> PendingRunsOut:
    """Count the runs PENDING review that reviewers find in the NRT and Quality Gate lists.

    Same rules as the lists (`get_nrts`): finished in the last `NRT_LIST_WINDOW_DAYS` days;
    NRT runs are those whose origin is neither TAS nor QG, Quality Gate runs have origin QG.
    TAS runs are excluded: they are read-only in the portal, so nobody can review them.
    """
    try:
        is_qg = TestRun.test_origin == "QG"
        is_nrt = TestRun.test_origin.not_in(("TAS", "QG"))
        row = (
            await db.execute(
                select(
                    func.count().filter(is_nrt).label("nrt"),
                    func.count().filter(is_qg).label("qg"),
                ).where(
                    TestRun.revision_result == RevisionResultEnum.PENDING,
                    TestRun.test_origin.is_not(None),
                    TestRun.timestamp_end > _list_window_start(),
                )
            )
        ).one()
        return PendingRunsOut(total=row.nrt + row.qg, nrt=row.nrt, qg=row.qg)
    except Exception:
        logger.exception("get_pending_runs failed")
        raise


async def get_avg_pass_rate(db: AsyncSession) -> int | None:
    try:
        averages = (
            select(
                TestSuite.test_object,
                func.avg(TestRun.passed_scenario * 100.0 / TestRun.scenario_qty).label("suite_avg"),
            )
            .join(TestSuite, TestSuite.id == TestRun.suite_id)
            .where(
                TestRun.scenario_qty > 0,
                TestRun.test_origin.is_not(None),
                TestRun.test_origin.not_in(("TAS", "QG", "QA_GATE")),
            )
            .group_by(TestSuite.test_object)
            .cte("averages")
        )
        stmt = select(func.avg(averages.c.suite_avg))
        result = await db.execute(stmt)
        avg_rate = result.scalar()
        return round(float(avg_rate), 0) if avg_rate is not None else None
    except Exception:
        logger.exception("get_avg_pass_rate failed")
        raise
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DatabaseError, DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

# to import models
from app.models.test_metrics import FlakyScenario, TestExecution, TestRun, TestSuite
from app.schemas.test_metrics import (
    FlakyScenarioIn,
    NrtDetailsObject,
    NrtListObject,
    TestExecutionOut,
    TestRunOut,
    TestSuiteOut,
    UpdateNrtsObject,
)

logger = structlog.get_logger(__name__)

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
            select(TestSuite).filter_by(test_object=test_object_in).order_by(TestSuite.suite_version.desc()).limit(1)
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

async def get_nrts(db: AsyncSession, test_object: str | None, origin: str | None, page:int = 1, size:int = 20) -> list[NrtListObject]:
    try:
        stmt = (
            select(TestRun, TestSuite)
            .join(TestSuite, TestRun.suite_id == TestSuite.id)
            .where(TestRun.timestamp_end > datetime.now(timezone.utc) - timedelta(days=30))
            .order_by(TestRun.timestamp_end.desc())
            .offset((page - 1) * size)
            .limit(size)
        )

        count_stmt = (
                select(func.count()).select_from(TestRun).where(TestRun.timestamp_end > datetime.now(timezone.utc) - timedelta(days=30))
        )

        if test_object:
            stmt = stmt.where(TestSuite.test_object == test_object)
            count_stmt = count_stmt.join(TestSuite, TestRun.suite_id == TestSuite.id).where(TestSuite.test_object == test_object)

        if origin:
            count_stmt = count_stmt.where(TestRun.test_origin == origin)
            stmt = stmt.where(TestRun.test_origin == origin)
        else:
            count_stmt = count_stmt.where(TestRun.test_origin != "TAS")
            stmt = stmt.where(TestRun.test_origin != "TAS")

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
                    include={"id", "revisioned", "revision_date", "revisioned_by", "revision_result", "product_issue","qa_issue","product_issue_status","qa_issue_status"},
                    exclude_unset=True,
                )
            )
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


async def get_flaky_scenarios(db: AsyncSession, test_object: str) -> list[FlakyScenario] | None:
    try:
        stmt = select(FlakyScenario).where(FlakyScenario.is_flaky == True)

        if test_object:
            stmt = stmt.where(FlakyScenario.test_object == test_object)

        result = await db.execute(stmt)
        scenarios = result.scalars().all() if result is not None else None
        if not scenarios:
            logger.info("get_flaky_scenarios query succeeded with 0 results", test_object=test_object)
        return scenarios
    except Exception:
        logger.exception("get_flaky_scenarios failed")
        raise


async def save_or_update_flaky_scenarios(db: AsyncSession, flaky_scenarios: list[FlakyScenarioIn]) -> list[FlakyScenario] | None:
    try:
        if not flaky_scenarios:
            logger.info("save_or_update_flaky_scenarios: no input records to save")
            return None
        processed_scenarios = []
        for scenario in flaky_scenarios:
            existing_scenario = await db.execute(
                select(FlakyScenario).where(
                    FlakyScenario.scenario_name == scenario.scenario_name,
                    FlakyScenario.suite_id == scenario.suite_id,
                    FlakyScenario.test_version == scenario.test_version,
                )
            )
            existing_scenario = existing_scenario.scalars().first()
            values = scenario.model_dump(by_alias=False, exclude={"id"})
            if existing_scenario:
                for key, value in values.items():
                    setattr(existing_scenario, key, value)
                processed_scenarios.append(existing_scenario)
            else:
                logger.info("save_or_update_flaky_scenarios query succeeded with 0 results; inserting scenario", suite_id=str(scenario.suite_id), scenario_name=scenario.scenario_name, test_version=scenario.test_version)
                model = FlakyScenario(**values)
                model.inserted_at = datetime.now()
                db.add(model)
                processed_scenarios.append(model)
        await db.commit()
        for scenario in processed_scenarios:
            await db.refresh(scenario)
        return processed_scenarios
    except Exception:
        logger.exception("save_or_update_flaky_scenarios failed")
        raise

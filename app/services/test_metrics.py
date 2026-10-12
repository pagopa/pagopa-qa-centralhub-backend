from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, DatabaseError
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta, timezone,date


# to import models
from app.models.test_metrics import TestSuite, TestRun, TestExecution
from app.schemas.test_metrics import NrtListObject

async def create_test_suite(db: AsyncSession, test_suite: TestSuite):
    db.add(test_suite)
    await db.commit()
    await db.refresh(test_suite)
    return test_suite
    

async def get_latest_test_suite_version(db: AsyncSession, test_object_in: str) -> TestSuite:
    result = await db.execute(
        select(TestSuite).filter_by(test_object=test_object_in).order_by(TestSuite.suite_version.desc()).limit(1)
    )
    return result.scalar_one_or_none()

async def create_test_run(db: AsyncSession, test_run: TestRun):
    db.add(test_run)
    await db.commit()
    await db.refresh(test_run)
    return test_run

async def create_test_execution(db: AsyncSession, test_execution: list[TestExecution]):
    db.add_all(test_execution)
    await db.commit()
    for te in test_execution:
        await db.refresh(te)
    return test_execution

async def get_nrts(db: AsyncSession, test_object: str | None) -> list[NrtListObject]:
    stmt = (
        select(TestRun, TestSuite)
        .join(TestSuite, TestRun.suite_id == TestSuite.id)
        .where(TestRun.timestamp_end > datetime.now(timezone.utc) - timedelta(days=30))
        .order_by(TestRun.timestamp_end.desc()).limit(30)
    )
    if test_object:
        stmt = stmt.where(TestSuite.test_object == test_object)

    result = await db.execute(stmt)
    return [
        NrtListObject(
            run_id=run.id,
            date=date.fromtimestamp(run.timestamp_end.timestamp()).strftime("%Y-%m-%d"),
            test_object=suite.test_object,
            total_scenarios=run.scenario_qty,
            passed_scenarios=run.passed_scenario,
            failed_scenarios=run.failed_scenario,
            broken_scenarios=run.broken_scenario,
            skipped_scenarios=run.skipped_scenario,
            trigger_type=run.trigger_type.value if run.trigger_type else None,
            origin=run.test_origin,
        )
        for run, suite in result.all()
    ]
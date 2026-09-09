from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# to import models
# from app.models.test_metrics import TestSuite, TestRun, TestExecution

async def create_test_suite(db: AsyncSession, test_suite: TestSuite):
    db.add(test_suite)
    await db.commit()
    await db.refresh(test_suite)
    return test_suite


async def get_latest_test_suite_version(db: AsyncSession, test_object_in: str) -> TestSuite:
    result = await db.execute(
        select(TestSuite).filter_by(test_object=test_object_in).order_by(TestSuite.version.desc()).limit(1)
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
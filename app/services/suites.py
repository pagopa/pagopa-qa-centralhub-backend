from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.test_metrics import TestSuite, TestRun


async def get_suites(db: AsyncSession, trigger: str | None = None) -> list[str]:
    stmt = select(TestSuite.test_object).distinct().order_by(TestSuite.test_object)
    if trigger:
        stmt = stmt.join(TestRun, TestSuite.id == TestRun.suite_id).where(TestRun.trigger_type == trigger)
    result = await db.execute(stmt)
    return list(result.scalars().all())
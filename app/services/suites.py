from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.test_metrics import TestSuite, TestRun


async def get_suites(db: AsyncSession, origin: str | None = None) -> list[str]:
    stmt = select(TestSuite.test_object).distinct().order_by(TestSuite.test_object)
    
    if origin:
        stmt = stmt.join(TestRun, TestSuite.id == TestRun.suite_id).where(TestRun.test_origin == origin)
    else:
        stmt = stmt.join(TestRun, TestSuite.id == TestRun.suite_id).where(TestRun.test_origin != "TAS")
        
    result = await db.execute(stmt)
    return list(result.scalars().all())
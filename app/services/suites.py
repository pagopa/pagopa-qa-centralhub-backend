import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_metrics import TestRun, TestSuite

logger = structlog.get_logger(__name__)


async def get_suites(db: AsyncSession, origin: str | None = None) -> list[str]:
    try:
        stmt = select(TestSuite.test_object).distinct().order_by(TestSuite.test_object)
        
        if origin:
            stmt = stmt.join(TestRun, TestSuite.id == TestRun.suite_id).where(TestRun.test_origin == origin)
        else:
            stmt = stmt.join(TestRun, TestSuite.id == TestRun.suite_id).where(TestRun.test_origin != "TAS")
            
        result = await db.execute(stmt)
        suites = list(result.scalars().all())
        if not suites:
            logger.info("get_suites query succeeded with 0 results")
        return suites
    except Exception:
        logger.exception("get_suites failed")
        raise

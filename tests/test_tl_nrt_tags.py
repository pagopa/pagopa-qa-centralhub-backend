# ruff: noqa: S101, F811
"""Fase 4, Task 1: tag delle esecuzioni estratti dal report Allure."""
from __future__ import annotations

from sqlalchemy import select

from app.models.test_lifecycle import TlScenarioAlias
from app.models.test_metrics import TestExecution as Execution
from tests._db import TestSession
from tests.tl_nrt_fixtures import nrt  # noqa: F401


async def _tags(run_id):
    async with TestSession() as s:
        stmt = select(Execution.tags).where(Execution.run_id == run_id)
        rows = (await s.execute(stmt)).scalars()
        return list(rows)


async def test_tags_extracted_from_allure_labels(nrt):
    run_id = await nrt.run(executions=[("FDR3_001_01", "PASSED"), (None, "FAILED")])
    assert sorted(await _tags(run_id), key=len) == [[], ["FDR3_001_01"]]


async def test_execution_without_report_has_no_tags(nrt):
    run_id = await nrt.run()
    async with TestSession() as s:
        s.add(Execution(run_id=run_id, scenario_name="x"))
        await s.commit()
    assert await _tags(run_id) == [[]]


def test_alias_model_exists():
    assert TlScenarioAlias.__table__.c.alias.unique

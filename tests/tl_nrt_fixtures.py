"""Run ed esecuzioni NRT per i test della fase 4 (rimosse a fine test)."""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete

from app.models.test_metrics import (
    EnvEnum,
    ScenarioStatusEnum,
    TestExecution,
    TestRun,
    TestSuite,
)
from tests._db import TestSession


class NrtData:
    def __init__(self) -> None:
        self.suite_id: uuid.UUID | None = None
        self.base = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
        self._n = 0

    async def suite(self) -> uuid.UUID:
        if self.suite_id is None:
            async with TestSession() as s:
                suite = TestSuite(
                    test_object=f"tl-nrt-{uuid.uuid4().hex[:8]}", test_type="NRT",
                    test_object_version="1",
                )
                s.add(suite)
                await s.commit()
                self.suite_id = suite.id
        return self.suite_id

    async def run(self, env: str = "UAT", executions: list[tuple[str | None, str]] = (),
                  name: str = "Scenario", **report: Any) -> uuid.UUID:
        """Una run con esecuzioni (tag senza @ o None, stato)."""
        suite_id = await self.suite()
        self._n += 1
        async with TestSession() as s:
            run = TestRun(
                suite_id=suite_id, env=EnvEnum[env],
                timestamp_start=self.base + timedelta(hours=self._n),
                allure_url="https://allure.test/run", test_origin="NRT",
            )
            s.add(run)
            await s.flush()
            for tag, status in executions:
                labels = [{"name": "severity", "value": "normal"}]
                if tag:
                    labels.append({"name": "tag", "value": tag})
                s.add(TestExecution(
                    run_id=run.id, status=ScenarioStatusEnum[status],
                    scenario_name=report.get("scenario_name", name),
                    allure_report={"name": name, "labels": labels},
                    allure_url="https://allure.test/case",
                ))
            await s.commit()
            return run.id

    async def cleanup(self) -> None:
        if self.suite_id is not None:
            async with TestSession() as s:
                await s.execute(delete(TestSuite).where(TestSuite.id == self.suite_id))
                await s.commit()


@pytest.fixture
async def nrt() -> AsyncIterator[NrtData]:
    data = NrtData()
    yield data
    await data.cleanup()

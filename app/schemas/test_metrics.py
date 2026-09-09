from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


class EnvEnum(str, Enum):
    DEV = "DEV"
    UAT = "UAT"
    PROD = "PROD"


class TriggerTypeEnum(str, Enum):
    MANUAL = "MANUAL"
    CRON = "CRON"
    CI_PIPELINE = "CI_PIPELINE"


class ScenarioStatusEnum(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    BROKEN = "BROKEN"
    SKIPPED = "SKIPPED"


class TestSuiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    test_object: str
    test_type: str
    suite_version: str
    owner_team: str | None = None


class TestSuiteCreate(BaseModel):
    test_object: str
    test_type: str
    suite_version: str
    owner_team: str | None = None


class TestRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    suite_id: uuid.UUID
    scenario_qty: int | None = None
    passed_scenario: int | None = None
    failed_scenario: int | None = None
    broken_scenario: int | None = None
    timestamp_start: datetime | None = None
    timestamp_end: datetime | None = None
    duration_ms: int | None = None
    env: EnvEnum | None = None
    trigger_type: TriggerTypeEnum | None = None
    test_version: str | None = None


class TestRunCreate(BaseModel):
    suite_id: uuid.UUID
    scenario_qty: int | None = None
    passed_scenario: int | None = None
    failed_scenario: int | None = None
    broken_scenario: int | None = None
    timestamp_start: datetime | None = None
    timestamp_end: datetime | None = None
    duration_ms: int | None = None
    env: EnvEnum | None = None
    trigger_type: TriggerTypeEnum | None = None
    test_version: str | None = None


class TestExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    allure_id: str | None = None
    status: ScenarioStatusEnum | None = None
    scenario_name: str | None = None
    allure_report: dict[str, Any] | None = None
    duration_ms: int | None = None
    error_message: str | None = None
    retries: int | None = None


class TestExecutionCreate(BaseModel):
    run_id: uuid.UUID
    allure_id: str | None = None
    status: ScenarioStatusEnum | None = None
    scenario_name: str | None = None
    allure_report: dict[str, Any] | None = None
    duration_ms: int | None = None
    error_message: str | None = None
    retries: int | None = None


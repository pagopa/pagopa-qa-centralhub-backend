from __future__ import annotations

import uuid
from datetime import datetime, date
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Optional


class EnvEnum(str, Enum):
    DEV = "DEV"
    UAT = "UAT"
    PROD = "PROD"


class TriggerTypeEnum(str, Enum):
    MANUAL = "MANUAL"
    CRON = "CRON"
    CI_PIPELINE = "CI_PIPELINE"
    TAS = "TAS"
    QA_GATE = "QA_GATE"


class ScenarioStatusEnum(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    BROKEN = "BROKEN"
    SKIPPED = "SKIPPED"

class RevisionResultEnum(str, Enum):
    OK = "OK"
    KO = "KO"
    PENDING = "PENDING"


class TestSuiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    test_object: str
    test_type: str
    test_object_version: str
    owner_team: str | None = None


class TestSuiteCreate(BaseModel):
    test_object: str
    test_type: str
    test_object_version: str
    owner_team: str | None = None

class TestRunCreate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    suite_id: str
    scenario_qty: int | None = None
    passed_scenario: int | None = None
    failed_scenario: int | None = None
    broken_scenario: int | None = None
    skipped_scenario: int | None = None
    timestamp_start: datetime | None = None
    timestamp_end: datetime | None = None
    duration_ms: int | None = None
    env: str | None = None
    trigger_type: str | None = None
    test_version: str | None = None
    test_origin: str | None = None
    revisioned: bool | None = False
    revision_date: datetime | None = None
    revisioned_by: str | None = None
    revision_result: RevisionResultEnum | None = None
    allure_url: str | None = None
    product_issue: str | None = None
    product_issue_status: str | None = None
    qa_issue: str | None = None
    qa_issue_status: str | None = None
    gha_run_url: str | None = None
    note: str | None = None

class TestRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    suite_id: uuid.UUID
    scenario_qty: int | None = None
    passed_scenario: int | None = None
    failed_scenario: int | None = None
    broken_scenario: int | None = None
    skipped_scenario: int | None = None
    timestamp_start: datetime | None = None
    timestamp_end: datetime | None = None
    duration_ms: int | None = None
    env: EnvEnum | None = None
    trigger_type: TriggerTypeEnum | None = None
    test_version: str | None = None
    test_origin: str | None = None
    revisioned: bool | None = False
    revision_date: datetime | None = None
    revisioned_by: str | None = None
    revision_result: RevisionResultEnum | None = None
    allure_url: str | None = None
    product_issue: str | None = None
    product_issue_status: str | None = None
    qa_issue: str | None = None
    qa_issue_status: str | None = None
    gha_run_url: str | None = None
    note: str | None = None


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
    revision_note: str | None = None
    allure_url: str | None = None

class TestExecutionCreate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: uuid.UUID
    allure_id: str | None = None
    status: str | None = None
    scenario_name: str | None = None
    allure_report: dict[str, Any] | None = None
    duration_ms: int | None = None
    error_message: str | None = None
    retries: int | None = None
    revision_note: str | None = None
    allure_url: str | None = None


class NrtListObject(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    test_run: TestRunOut | None = None
    test_suite: TestSuiteOut | None = None
    

class NrtDetailsObject(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    test_suite: TestSuiteOut | None = None
    test_run: TestRunOut | None = None
    scenarios: list[TestExecutionOut] | None = None

class UpdateNrtsObject(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    test_suite: TestSuiteOut | None = None
    test_runs: list[TestRunOut | None] | None = None
    scenarios: list[TestExecutionOut] | None = None

class FlakyScenarioIn(BaseModel):
    """Flag sent by the NRT/TAS/QG report panels.

    Flags belong to the scenario (test_object + test_type + scenario_name): version fields are
    accepted for compatibility, and test_type is resolved from suite_id when missing.
    """

    model_config = ConfigDict(from_attributes=True)

    id: Optional[uuid.UUID] = None
    suite_id: uuid.UUID | None = None
    test_object: str
    test_type: str | None = None
    scenario_name: str
    test_object_version: str | None = None
    test_version: str | None = None
    inserted_at: datetime | None = None
    is_flaky: bool = Field(default=True)
    user: str | None = None

    @model_validator(mode="after")
    def _needs_test_type_or_suite(self) -> FlakyScenarioIn:
        if not self.test_type and self.suite_id is None:
            raise ValueError("A flaky scenario needs a test_type or suite_id")
        return self

class FlakyScenarioOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[uuid.UUID] = None
    suite_id: uuid.UUID | None = None
    test_object: str
    test_type: str
    scenario_name: str
    test_object_version: str | None = None
    test_version: str | None = None
    inserted_at: datetime | None = None
    is_flaky: bool

class NrtWithLatestRunOut(BaseModel):
    suite: TestSuiteOut
    latest_run: TestRunOut | None
    trend: list[float]  # pass rate 0.0–1.0, ultimi N run, ordine cronologico


class PendingRunsOut(BaseModel):
    """Runs waiting for review (revision result PENDING) that reviewers can act on in the portal."""

    total: int
    nrt: int
    qg: int

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FlakyHint = Literal["to_mark", "to_unmark", "broken"]
FlakyFlagFilter = Literal["all", "flagged", "unflagged"]
FlakyState = Literal["OK", "INSUFFICIENT_DATA", "BROKEN"]


class OutcomePointOut(BaseModel):
    run_id: uuid.UUID
    ts: datetime
    outcome: Literal["P", "R", "F"]


class FlakyScenarioRowOut(BaseModel):
    test_object: str
    test_type: str
    scenario_name: str
    env: str
    score: int | None
    level: int | None
    state: FlakyState
    flip_rate: float | None
    retry_pass_rate: float | None
    runs_analyzed: int
    last_failure_at: datetime | None
    recent_outcomes: list[OutcomePointOut]
    last_suite_id: uuid.UUID | None
    is_flaky: bool
    flagged_by: str | None
    flagged_at: datetime | None
    hint: FlakyHint | None


class FlakySummaryOut(BaseModel):
    to_mark: int
    to_unmark: int
    broken: int
    flagged: int
    analyzed: int


class FlakyHistoryItemOut(BaseModel):
    run_id: uuid.UUID
    ts: datetime
    outcome: Literal["P", "R", "F"]
    status: str
    retries: int | None
    error_message: str | None
    allure_url: str | None
    test_version: str | None
    test_object_version: str
    test_origin: str | None


class FlakyFlagIn(BaseModel):
    test_object: str = Field(min_length=1)
    test_type: str = Field(min_length=1)
    scenario_name: str = Field(min_length=1)
    is_flaky: bool
    user: str = Field(min_length=1)


class FlakyFlagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    test_object: str
    test_type: str
    scenario_name: str
    is_flaky: bool
    flagged_by: str | None
    flagged_at: datetime | None
    updated_by: str | None
    updated_at: datetime


class FlakyRecomputeIn(BaseModel):
    user: str = Field(min_length=1)


class FlakyRecomputeStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    last_attempt_at: datetime
    last_success_at: datetime | None
    last_error: str | None
    last_trigger: str | None
    last_requested_by: str | None
    scored_count: int

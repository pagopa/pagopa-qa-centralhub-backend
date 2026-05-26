from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SuiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    display_name: str
    suite_path: str
    github_repo: str
    enabled: bool
    last_synced_at: datetime | None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    suite_id: uuid.UUID
    run_at: datetime
    passed: int
    failed: int
    skipped: int
    duration_ms: int
    allure_url: str
    status: str
    synced_at: datetime


class RunWithSuiteOut(RunOut):
    suite_name: str
    suite_display_name: str


class SuiteWithLatestRunOut(BaseModel):
    suite: SuiteOut
    latest_run: RunOut | None


class SyncResponse(BaseModel):
    status: str
    message: str

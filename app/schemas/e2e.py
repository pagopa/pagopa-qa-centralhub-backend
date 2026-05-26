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
    sync_lookback_days: int | None
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
    trend: list[float]  # pass rate 0.0–1.0, ultimi N run, ordine cronologico


class SuiteUpdate(BaseModel):
    display_name: str | None = None
    suite_path: str | None = None
    github_repo: str | None = None
    enabled: bool | None = None
    sync_lookback_days: int | None = None


class SuiteCreate(BaseModel):
    name: str
    display_name: str
    suite_path: str
    github_repo: str = "pagopa/pagopa-platform-integration-test"
    enabled: bool = True
    sync_lookback_days: int | None = None


class SyncResponse(BaseModel):
    status: str
    message: str

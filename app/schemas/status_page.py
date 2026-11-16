from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.services.status_page_versions import Drift

RunKind = Literal["NRT", "QG", "TAS"]
RunOutcome = Literal["PASSED", "FAILED", "NEUTRAL"]
EnvTarget = Literal["DEV", "UAT", "PROD"]
AnyTarget = Literal["REPO", "DEV", "UAT", "PROD"]
AppFlag = Literal["ALIGNED", "DRIFT", "UNREACHABLE", "UNTESTED"]


class SyncStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: str
    trigger: str
    requested_by: str | None
    queued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    last_success_at: datetime | None
    applications_checked: int
    reads_failed: int
    errors: list[object]


class RunRefOut(BaseModel):
    run_id: uuid.UUID
    kind: RunKind
    outcome: RunOutcome
    needs_review: bool
    trigger_type: str | None
    timestamp_start: datetime | None
    passed: int | None
    failed: int | None
    allure_url: str | None
    gha_run_url: str | None
    hub_url: str


class CellOut(BaseModel):
    version: str | None
    read_status: str
    error_detail: str | None
    checked_at: datetime
    last_ok_at: datetime | None
    version_since: datetime | None
    drift: Drift | None = None
    tests: list[RunRefOut] = []


class ApplicationRowOut(BaseModel):
    id: uuid.UUID
    name: str
    product_id: uuid.UUID
    product_key: str
    repository: str
    pipeline_url: str | None
    runtime: str
    ci: str
    kind: str
    test_object: str | None
    ea_node_id: str | None
    repo: CellOut | None
    envs: dict[EnvTarget, CellOut | None]
    flags: list[AppFlag]


class ProductGroupOut(BaseModel):
    id: uuid.UUID
    name: str
    applications: list[ApplicationRowOut]


class AreaGroupOut(BaseModel):
    area: str
    products: list[ProductGroupOut]


class StatusOut(BaseModel):
    sync: SyncStatusOut | None
    areas: list[AreaGroupOut]


class SummaryOut(BaseModel):
    total: int
    aligned: int
    drift: int
    unreachable: int
    untested: int
    last_success_at: datetime | None


class DeployEventOut(BaseModel):
    version: str
    previous_version: str | None
    detected_at: datetime
    previous_seen_at: datetime | None
    is_baseline: bool
    direction: Literal["upgrade", "rollback", "unknown", "baseline"]
    runs: list[RunRefOut] = []


class ApplicationDetailOut(BaseModel):
    application: ApplicationRowOut
    product_name: str
    history: dict[AnyTarget, list[DeployEventOut]]

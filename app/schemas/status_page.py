from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

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


# --- Catalog management -------------------------------------------------------------------------

Area = Literal["CORE", "TOUCHPOINT"]
Runtime = Literal["AKS", "AZ"]
Ci = Literal["GHA", "AZDO"]
AppKind = Literal["backend", "frontend"]

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Repository = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
]


def _blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _check_https(value: str | None) -> str | None:
    if value is not None and not value.startswith("https://"):
        raise ValueError("pipeline_url must start with https://")
    return value


class ProductIn(BaseModel):
    name: Text
    area: Area
    user: Text


class ProductPatch(BaseModel):
    name: Text | None = None
    area: Area | None = None
    user: Text


class ProductOut(BaseModel):
    id: uuid.UUID
    name: str
    area: str
    application_count: int


class ApplicationIn(BaseModel):
    product_id: uuid.UUID
    name: Text
    product_key: Text
    repository: Repository
    pipeline_url: Text | None = None
    runtime: Runtime
    ci: Ci
    kind: AppKind = "backend"
    test_object: Text | None = None
    ea_node_id: Text | None = None
    user: Text

    _blank = field_validator("pipeline_url", "test_object", "ea_node_id", mode="before")(
        _blank_to_none
    )
    _https = field_validator("pipeline_url")(_check_https)


class ApplicationPatch(BaseModel):
    """Partial update: only the fields actually sent are changed (nullable ones may be cleared)."""

    product_id: uuid.UUID | None = None
    name: Text | None = None
    product_key: Text | None = None
    repository: Repository | None = None
    pipeline_url: Text | None = None
    runtime: Runtime | None = None
    ci: Ci | None = None
    kind: AppKind | None = None
    test_object: Text | None = None
    ea_node_id: Text | None = None
    user: Text

    _blank = field_validator("pipeline_url", "test_object", "ea_node_id", mode="before")(
        _blank_to_none
    )
    _https = field_validator("pipeline_url")(_check_https)


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    name: str
    product_key: str
    repository: str
    pipeline_url: str | None
    runtime: str
    ci: str
    kind: str
    test_object: str | None
    ea_node_id: str | None


class CheckIn(BaseModel):
    product_key: Text
    repository: Repository


class CheckTargetOut(BaseModel):
    ok: bool
    version: str | None
    detail: str | None


class CheckOut(BaseModel):
    repo: CheckTargetOut
    envs: dict[str, CheckTargetOut]


class EaComponentOut(BaseModel):
    id: str
    name: str


class DeleteIn(BaseModel):
    user: Text


class ManualSyncIn(BaseModel):
    user: Text

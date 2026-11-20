from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    StringConstraints,
    computed_field,
    field_validator,
)

from app.services.status_page_sources import is_valid_repository
from app.services.status_page_sync import is_in_progress
from app.services.status_page_versions import Drift

RunKind = Literal["NRT", "QG", "TAS"]
RunOutcome = Literal["PASSED", "FAILED", "NEUTRAL"]
EnvTarget = Literal["DEV", "UAT", "PROD"]
AnyTarget = Literal["REPO", "DEV", "UAT", "PROD"]
AppFlag = Literal["ALIGNED", "DRIFT", "UNREACHABLE", "UNTESTED", "UNDER_QA"]


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_progress(self) -> bool:
        """QUEUED or RUNNING and not stale (QUEUED > 10 min, RUNNING > 30 min)."""
        return is_in_progress(self)


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


class StackOut(BaseModel):
    language: str
    version: str | None
    source: str | None
    ref: str | None


class ChangeItemOut(BaseModel):
    kind: str
    number: int | None
    title: str
    author: str | None
    url: str


class ChangeSummaryOut(BaseModel):
    status: Literal["OK", "UNAVAILABLE", "ERROR", "PENDING"]
    direction: str | None
    items: list[ChangeItemOut]
    total_commits: int
    truncated: bool
    compare_url: str | None


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
    version_source: str
    hidden: bool
    repo: CellOut | None
    envs: dict[EnvTarget, CellOut | None]
    flags: list[AppFlag]
    stack: StackOut | None = None


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
    under_qa: int
    last_success_at: datetime | None


class DeployEventOut(BaseModel):
    version: str
    previous_version: str | None
    detected_at: datetime
    previous_seen_at: datetime | None
    is_baseline: bool
    direction: Literal["upgrade", "rollback", "unknown", "baseline"]
    runs: list[RunRefOut] = []
    changes: ChangeSummaryOut | None = None


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


def _check_repository(value: str) -> str:
    if not is_valid_repository(value):
        raise ValueError("Nome repository non valido")
    return value


Repository = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^[A-Za-z0-9._-]+$"),
    AfterValidator(_check_repository),
]


def _blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _check_https(value: str | None) -> str | None:
    if value is not None and not value.startswith("https://"):
        raise ValueError("pipeline_url must start with https://")
    return value


VersionSource = Literal["AUTO", "MANUAL"]
ManualVersion = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]


class ManualVersionsIn(BaseModel):
    """Manually entered versions; only the keys actually sent are applied (null/empty clears)."""

    DEV: ManualVersion | None = None
    UAT: ManualVersion | None = None
    PROD: ManualVersion | None = None


class ProductIn(BaseModel):
    name: Text
    area: Area
    hidden: bool = False
    user: Text


class ProductPatch(BaseModel):
    name: Text | None = None
    area: Area | None = None
    hidden: bool | None = None
    user: Text


class ProductOut(BaseModel):
    id: uuid.UUID
    name: str
    area: str
    hidden: bool
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
    hidden: bool = False
    version_source: VersionSource = "AUTO"
    manual_versions: ManualVersionsIn | None = None
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
    hidden: bool | None = None
    version_source: VersionSource | None = None
    manual_versions: ManualVersionsIn | None = None
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
    hidden: bool
    version_source: str
    manual_versions: dict[EnvTarget, str | None] | None = None


class CheckIn(BaseModel):
    product_key: Text
    repository: Repository
    # Manual applications have no /info endpoint: probe the repository only.
    repo_only: bool = False


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


class VersionOut(BaseModel):
    """Last known version of an application in one environment (from ``sp_version_state``).

    ``version`` is the last good read: it is kept when a later read fails, so callers
    should also look at ``read_status`` and ``checked_at`` to judge how fresh it is.
    """

    product_key: str
    env: EnvTarget
    version: str
    read_status: str
    error_detail: str | None
    checked_at: datetime
    last_ok_at: datetime | None
    version_since: datetime | None


class DeleteIn(BaseModel):
    user: Text


class ManualSyncIn(BaseModel):
    user: Text

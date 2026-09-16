from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.sanp_health import (
    SanpEnvironment,
    SanpImportStatus,
    SanpResultStatus,
    SanpSeverity,
)


class SanpHealthChangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    level: int
    severity: SanpSeverity
    rule_id: str
    message: str
    path: str | None
    operation: str | None
    section: str | None
    comment: str | None
    change_order: int


class SanpHealthRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    github_run_id: int
    run_number: int
    head_sha: str
    workflow_branch: str
    conclusion: str
    html_url: str
    started_at: datetime
    completed_at: datetime
    synced_at: datetime
    import_status: SanpImportStatus
    error_message: str | None
    error_count: int
    warning_count: int
    info_count: int


class SanpHealthMatrixCellOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID | None
    environment: SanpEnvironment
    status: SanpResultStatus
    source_branch: str | None
    target_apim: str | None
    display_name: str | None
    description: str | None
    error_count: int
    warning_count: int
    info_count: int
    changes: list[SanpHealthChangeOut]

    @classmethod
    def not_configured(cls, environment: SanpEnvironment) -> SanpHealthMatrixCellOut:
        return cls(
            id=None,
            environment=environment,
            status=SanpResultStatus.NOT_CONFIGURED,
            source_branch=None,
            target_apim=None,
            display_name=None,
            description=None,
            error_count=0,
            warning_count=0,
            info_count=0,
            changes=[],
        )


class SanpHealthMatrixRowOut(BaseModel):
    spec_name: str
    display_name: str
    description: str
    sanp: SanpHealthMatrixCellOut
    collaudo: SanpHealthMatrixCellOut
    produzione: SanpHealthMatrixCellOut


class SanpHealthReportDetailOut(BaseModel):
    report: SanpHealthRunOut
    sanp_version: str | None
    items: list[SanpHealthMatrixRowOut]


class SanpHealthReportsResponse(BaseModel):
    items: list[SanpHealthRunOut]


class SanpHealthLatestResponse(BaseModel):
    report: SanpHealthReportDetailOut | None
    is_stale: bool
    stale_reason: str | None


class SanpHealthSyncStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    last_attempt_at: datetime
    last_success_at: datetime | None
    last_error: str | None
    imported_run_count: int


class SanpHealthSyncResult(BaseModel):
    status: SanpImportStatus
    imported_run_count: int
    errors: list[str]
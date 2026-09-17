from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class SanpEnvironment(StrEnum):
    SANP = "SANP"
    COLLAUDO = "COLLAUDO"
    PRODUZIONE = "PRODUZIONE"


class SanpImportStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class SanpResultStatus(StrEnum):
    KO = "KO"
    WARNING = "WARNING"
    INFO = "INFO"
    OK = "OK"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class SanpSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


def _enum_values(enum_class: type[StrEnum]) -> list[str]:
    return [str(value.value) for value in enum_class]


class SanpHealthRun(Base):
    __tablename__ = "sanp_health_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    github_run_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    run_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    workflow_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    conclusion: Mapped[str] = mapped_column(String(50), nullable=False)
    html_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    import_status: Mapped[SanpImportStatus] = mapped_column(
        Enum(
            SanpImportStatus,
            name="sanp_import_status",
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    info_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    results: Mapped[list[SanpHealthResult]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SanpHealthResult(Base):
    __tablename__ = "sanp_health_results"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "spec_name",
            "environment",
            name="uq_sanp_health_results_run_spec_environment",
        ),
        Index("ix_sanp_health_results_environment_status", "environment", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sanp_health_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    spec_name: Mapped[str] = mapped_column(String(500), nullable=False)
    environment: Mapped[SanpEnvironment] = mapped_column(
        Enum(SanpEnvironment, name="sanp_environment", values_callable=_enum_values),
        nullable=False,
    )
    source_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    target_apim: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[SanpResultStatus] = mapped_column(
        Enum(SanpResultStatus, name="sanp_result_status", values_callable=_enum_values),
        nullable=False,
    )
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    info_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    run: Mapped[SanpHealthRun] = relationship(back_populates="results")
    changes: Mapped[list[SanpHealthChange]] = relationship(
        back_populates="result",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SanpHealthChange.change_order",
    )


class SanpHealthChange(Base):
    __tablename__ = "sanp_health_changes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sanp_health_results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[SanpSeverity] = mapped_column(
        Enum(SanpSeverity, name="sanp_severity", values_callable=_enum_values), nullable=False
    )
    rule_id: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation: Mapped[str | None] = mapped_column(String(20), nullable=True)
    section: Mapped[str | None] = mapped_column(String(255), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    change_order: Mapped[int] = mapped_column(Integer, nullable=False)

    result: Mapped[SanpHealthResult] = relationship(back_populates="changes")


class SanpHealthSyncStatus(Base):
    __tablename__ = "sanp_health_sync_status"
    __table_args__ = (CheckConstraint("id = 1", name="ck_sanp_health_sync_status_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    imported_run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
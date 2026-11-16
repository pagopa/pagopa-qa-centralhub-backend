from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

AREAS = ("CORE", "TOUCHPOINT")
TARGETS = ("REPO", "DEV", "UAT", "PROD")
READ_STATUSES = ("OK", "ERROR", "EMPTY_BODY", "NO_VERSION", "NO_RELEASE")
RUNTIMES = ("AKS", "AZ")
CIS = ("GHA", "AZDO")
KINDS = ("backend", "frontend")
SYNC_STATUSES = ("QUEUED", "RUNNING", "OK", "PARTIAL", "FAILED")
SYNC_TRIGGERS = ("SCHEDULED", "MANUAL")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SpProduct(Base):
    __tablename__ = "sp_products"
    __table_args__ = (CheckConstraint(_in("area", AREAS), name="ck_sp_products_area"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    area: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    applications: Mapped[list[SpApplication]] = relationship(
        back_populates="product", passive_deletes=True
    )


class SpApplication(Base):
    __tablename__ = "sp_applications"
    __table_args__ = (
        UniqueConstraint("product_id", "name", name="uq_sp_applications_product_name"),
        CheckConstraint(_in("runtime", RUNTIMES), name="ck_sp_applications_runtime"),
        CheckConstraint(_in("ci", CIS), name="ck_sp_applications_ci"),
        CheckConstraint(_in("kind", KINDS), name="ck_sp_applications_kind"),
        Index("ix_sp_applications_test_object", "test_object"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sp_products.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    product_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    repository: Mapped[str] = mapped_column(String(200), nullable=False)
    pipeline_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    runtime: Mapped[str] = mapped_column(String(10), nullable=False)
    ci: Mapped[str] = mapped_column(String(10), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(10), nullable=False, default="backend", server_default="backend"
    )
    test_object: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ea_node_id: Mapped[str | None] = mapped_column(
        String(200), ForeignKey("ea_nodes.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    product: Mapped[SpProduct] = relationship(back_populates="applications")


class SpVersionState(Base):
    __tablename__ = "sp_version_state"
    __table_args__ = (
        CheckConstraint(_in("target", TARGETS), name="ck_sp_version_state_target"),
        CheckConstraint(_in("read_status", READ_STATUSES), name="ck_sp_version_state_read_status"),
    )

    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sp_applications.id", ondelete="CASCADE"), primary_key=True
    )
    target: Mapped[str] = mapped_column(String(4), primary_key=True)
    version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    read_status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SpDeployEvent(Base):
    __tablename__ = "sp_deploy_events"
    __table_args__ = (
        CheckConstraint(_in("target", TARGETS), name="ck_sp_deploy_events_target"),
        Index("ix_sp_deploy_events_app_target_detected", "application_id", "target", "detected_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sp_applications.id", ondelete="CASCADE"), nullable=False
    )
    target: Mapped[str] = mapped_column(String(4), nullable=False)
    previous_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    previous_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_baseline: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class SpSyncStatus(Base):
    __tablename__ = "sp_sync_status"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_sp_sync_status_singleton"),
        CheckConstraint(_in("status", SYNC_STATUSES), name="ck_sp_sync_status_status"),
        CheckConstraint(_in("trigger", SYNC_TRIGGERS), name="ck_sp_sync_status_trigger"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    trigger: Mapped[str] = mapped_column(String(10), nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applications_checked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reads_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

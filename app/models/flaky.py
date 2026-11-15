"""Flaky scenarios: per-scenario flag registry, per-environment scores and recompute status.

A scenario is identified by test_object + test_type + scenario_name, so flags and scores
survive new product (suite) and test versions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.test_metrics import EnvEnum


class FlakyRegistry(Base):
    __tablename__ = "flaky_registry"
    __table_args__ = (
        UniqueConstraint(
            "test_object", "test_type", "scenario_name", name="uq_qachub_flaky_registry_key"
        ),
        {"schema": "qachub"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    test_object: Mapped[str] = mapped_column(String, nullable=False)
    test_type: Mapped[str] = mapped_column(String, nullable=False)
    scenario_name: Mapped[str] = mapped_column(String, nullable=False)
    is_flaky: Mapped[bool] = mapped_column(Boolean, nullable=False)
    flagged_by: Mapped[str | None] = mapped_column(String, nullable=True)
    flagged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    inserted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FlakyScore(Base):
    __tablename__ = "flaky_scores"
    __table_args__ = (
        Index("ix_qachub_flaky_scores_env_level", "env", "level"),
        {"schema": "qachub"},
    )

    test_object: Mapped[str] = mapped_column(String, primary_key=True)
    test_type: Mapped[str] = mapped_column(String, primary_key=True)
    scenario_name: Mapped[str] = mapped_column(String, primary_key=True)
    env: Mapped[EnvEnum] = mapped_column(
        Enum(EnvEnum, name="env_enum", schema="qachub", create_type=False), primary_key=True
    )
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    flip_rate: Mapped[float] = mapped_column(Numeric(5, 4, asdecimal=False), nullable=False)
    retry_pass_rate: Mapped[float] = mapped_column(Numeric(5, 4, asdecimal=False), nullable=False)
    runs_analyzed: Mapped[int] = mapped_column(Integer, nullable=False)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recent_outcomes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    last_suite_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qachub.test_suites.id", ondelete="SET NULL"), nullable=True
    )
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FlakyRecomputeStatus(Base):
    __tablename__ = "flaky_recompute_status"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_flaky_recompute_status_singleton"),
        {"schema": "qachub"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_trigger: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_requested_by: Mapped[str | None] = mapped_column(String, nullable=True)
    scored_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

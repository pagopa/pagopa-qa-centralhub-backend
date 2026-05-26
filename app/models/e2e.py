from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class E2eSuite(Base):
    __tablename__ = "e2e_suites"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    suite_path: Mapped[str] = mapped_column(String(200), nullable=False)
    github_repo: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_lookback_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    runs: Mapped[list[E2eRun]] = relationship(
        "E2eRun", back_populates="suite", cascade="all, delete-orphan"
    )


class E2eRun(Base):
    __tablename__ = "e2e_runs"
    __table_args__ = (UniqueConstraint("suite_id", "run_at", name="uq_e2e_run_suite_run_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    suite_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("e2e_suites.id"), nullable=False
    )
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    passed: Mapped[int] = mapped_column(Integer, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, nullable=False)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    allure_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # passed | failed | mixed
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    suite: Mapped[E2eSuite] = relationship("E2eSuite", back_populates="runs")

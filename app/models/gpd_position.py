from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Integer, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class GpdPositionSnapshot(Base):
    __tablename__ = "gpd_position_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)

    total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gpd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gpd_payable: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gpd4aca: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gpd4aca_payable: Mapped[int] = mapped_column(BigInteger, nullable=False)
    wisp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pa_create_position: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pa_create_position_payable: Mapped[int] = mapped_column(BigInteger, nullable=False)

    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GpdPositionSyncStatus(Base):
    __tablename__ = "gpd_position_sync_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

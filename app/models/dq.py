from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class DqCategory(str, enum.Enum):
    PUNTUALE = "puntuale"
    INTRA_ENTITA = "intra_entita"
    CROSS_ENTITA = "cross_entita"


class DqRiskLevel(str, enum.Enum):
    ALTO = "ALTO"
    MEDIO = "MEDIO"
    BASSO = "BASSO"


class DqControlStatus(str, enum.Enum):
    DA_IMPLEMENTARE = "da_implementare"
    IN_SVILUPPO = "in_sviluppo"
    ATTIVO = "attivo"
    NON_ATTIVO = "non_attivo"


dq_category_enum = ENUM(
    DqCategory, name="dq_category", create_type=False, values_callable=lambda e: [m.value for m in e]
)
dq_risk_level_enum = ENUM(
    DqRiskLevel, name="dq_risk_level", create_type=False, values_callable=lambda e: [m.value for m in e]
)
dq_control_status_enum = ENUM(
    DqControlStatus,
    name="dq_control_status",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)


class DqDimension(Base):
    __tablename__ = "dq_dimensions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    catalog_controls: Mapped[list["DqCatalogControl"]] = relationship(back_populates="dimension")


class DqCatalogControl(Base):
    __tablename__ = "dq_catalog_controls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category: Mapped[DqCategory] = mapped_column(dq_category_enum, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    dimension_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dq_dimensions.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    dimension: Mapped["DqDimension"] = relationship(back_populates="catalog_controls")
    instances: Mapped[list["DqControlInstance"]] = relationship(back_populates="catalog_control")


class DqDomain(Base):
    __tablename__ = "dq_domains"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    instances: Mapped[list["DqControlInstance"]] = relationship(back_populates="domain")


class DqControlInstance(Base):
    __tablename__ = "dq_control_instances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dq_domains.id", ondelete="CASCADE"), nullable=False
    )
    catalog_control_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dq_catalog_controls.id", ondelete="RESTRICT"), nullable=False
    )
    table_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    field_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    risk: Mapped[DqRiskLevel] = mapped_column(dq_risk_level_enum, nullable=False)
    impact: Mapped[DqRiskLevel] = mapped_column(dq_risk_level_enum, nullable=False)
    status: Mapped[DqControlStatus] = mapped_column(
        dq_control_status_enum, nullable=False, default=DqControlStatus.DA_IMPLEMENTARE
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    domain: Mapped["DqDomain"] = relationship(back_populates="instances")
    catalog_control: Mapped["DqCatalogControl"] = relationship(back_populates="instances")

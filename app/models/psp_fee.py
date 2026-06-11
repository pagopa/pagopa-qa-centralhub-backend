from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PspFeeService(Base):
    __tablename__ = "psp_fee_services"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Anagrafica PSP
    psp_id: Mapped[str] = mapped_column(String(50), nullable=False)
    psp_rag_soc: Mapped[str] = mapped_column(String(255), nullable=False)
    codice_abi: Mapped[str] = mapped_column(String(20), nullable=False)

    # Servizio
    nome_servizio: Mapped[str] = mapped_column(String(255), nullable=False)
    descrizione_canale_mod_pag: Mapped[str] = mapped_column(String(255), nullable=False)
    inf_desc_serv: Mapped[str] = mapped_column(String(255), nullable=False)
    inf_url_canale: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    url_informazioni_psp: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    tipo_vers_cod: Mapped[str] = mapped_column(String(10), nullable=False)
    canale_mod_pag: Mapped[str] = mapped_column(String(50), nullable=False)
    canale_mod_pag_code: Mapped[int] = mapped_column(Integer, nullable=False)

    # Commissioni / soglie
    importo_minimo: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    importo_massimo: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    costo_fisso: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)

    # Flag canali/metodi
    on_us: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    carte: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    conto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    altri_wisp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    altri_io: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    conto_app: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    carte_app: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_duplicated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PspFeeSyncStatus(Base):
    __tablename__ = "psp_fee_sync_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_run: Mapped[str] = mapped_column(String(20), nullable=False)
    notebook_version: Mapped[str] = mapped_column(String(20), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

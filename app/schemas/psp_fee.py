from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PspFeeServiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    psp_id: str
    psp_rag_soc: str
    codice_abi: str
    nome_servizio: str
    descrizione_canale_mod_pag: str
    inf_desc_serv: str
    inf_url_canale: str | None
    url_informazioni_psp: str | None
    tipo_vers_cod: str
    canale_mod_pag: str
    canale_mod_pag_code: int
    importo_minimo: float | None
    importo_massimo: float | None
    costo_fisso: float | None
    on_us: bool
    carte: bool
    conto: bool
    altri_wisp: bool
    altri_io: bool
    conto_app: bool
    carte_app: bool
    is_duplicated: bool


class PspFeeSyncStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    last_run: str
    notebook_version: str
    item_count: int
    synced_at: datetime


class PspFeeListResponse(BaseModel):
    items: list[PspFeeServiceOut]
    sync_status: PspFeeSyncStatusOut | None


class PspFeeSyncResponse(BaseModel):
    status: str
    item_count: int

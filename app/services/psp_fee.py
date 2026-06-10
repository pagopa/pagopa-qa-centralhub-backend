# qa-hub-backend/app/services/psp_fee.py
from __future__ import annotations

from decimal import Decimal

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.psp_fee import PspFeeService, PspFeeSyncStatus

PSP_FEE_FIELDS = [
    "psp_id", "psp_rag_soc", "codice_abi", "nome_servizio",
    "descrizione_canale_mod_pag", "inf_desc_serv", "inf_url_canale",
    "url_informazioni_psp", "importo_minimo", "importo_massimo", "costo_fisso",
    "canale_mod_pag_code", "tipo_vers_cod", "canale_mod_pag",
    "on_us", "carte", "conto", "altri_wisp", "altri_io",
    "is_duplicated", "conto_app", "carte_app",
]

DECIMAL_FIELDS = {"importo_minimo", "importo_massimo", "costo_fisso"}
NULLABLE_STRING_FIELDS = {"inf_url_canale", "url_informazioni_psp"}


def normalize_record(raw: dict) -> dict:
    record: dict = {}
    for field in PSP_FEE_FIELDS:
        value = raw.get(field)
        if field in NULLABLE_STRING_FIELDS and value == "None":
            value = None
        elif field in DECIMAL_FIELDS and value is not None:
            value = Decimal(str(value))
        record[field] = value
    return record


async def fetch_catalog() -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(settings.psp_fee_json_url)
        response.raise_for_status()
        return response.json()


async def sync_from_source(db: AsyncSession) -> int:
    data = await fetch_catalog()
    content = data.get("content")
    if not content:
        raise ValueError("PSP fee catalog response has no content")

    records = [PspFeeService(**normalize_record(raw)) for raw in content]

    await db.execute(delete(PspFeeService))
    db.add_all(records)

    sync_status = await db.get(PspFeeSyncStatus, 1)
    if sync_status is None:
        sync_status = PspFeeSyncStatus(id=1)
        db.add(sync_status)
    sync_status.last_run = data.get("last_Run", "")
    sync_status.notebook_version = data.get("notebookVersion", "")
    sync_status.item_count = len(records)

    await db.commit()
    return len(records)


async def list_services(db: AsyncSession) -> list[PspFeeService]:
    result = await db.scalars(select(PspFeeService).order_by(PspFeeService.psp_rag_soc))
    return list(result)


async def get_sync_status(db: AsyncSession) -> PspFeeSyncStatus | None:
    return await db.get(PspFeeSyncStatus, 1)

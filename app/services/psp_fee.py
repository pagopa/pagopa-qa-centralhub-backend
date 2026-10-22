# qa-hub-backend/app/services/psp_fee.py
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.psp_fee import PspFeeService, PspFeeSyncStatus

logger = structlog.get_logger(__name__)

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
    try:
        record: dict = {}
        for field in PSP_FEE_FIELDS:
            value = raw.get(field)
            if field in NULLABLE_STRING_FIELDS and value == "None":
                value = None
            elif field in DECIMAL_FIELDS and value is not None:
                value = Decimal(str(value))
            record[field] = value
        return record
    except Exception:
        logger.exception("normalize_record failed")
        raise


async def fetch_catalog() -> dict:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(settings.psp_fee_json_url)
            response.raise_for_status()
            _query_result_for_logging = response.json()
            if _query_result_for_logging is None or not _query_result_for_logging:
                logger.info("fetch_catalog query succeeded with 0 results")
            return _query_result_for_logging
    except Exception:
        logger.exception("fetch_catalog failed")
        raise


async def sync_from_source(db: AsyncSession) -> int:
    try:
        data = await fetch_catalog()
        content = data.get("content")
        if not content:
            logger.info("sync_from_source catalog query succeeded with 0 results")
            raise ValueError("PSP fee catalog response has no content")

        records = [PspFeeService(**normalize_record(raw)) for raw in content]

        await db.execute(delete(PspFeeService))
        db.add_all(records)

        sync_status = await db.get(PspFeeSyncStatus, 1)
        if sync_status is None or not sync_status:
            logger.info("sync_from_source query succeeded with 0 results")
        if sync_status is None:
            sync_status = PspFeeSyncStatus(id=1)
            db.add(sync_status)
        sync_status.last_run = data.get("last_Run", "")
        sync_status.notebook_version = data.get("notebookVersion", "")
        sync_status.item_count = len(records)
        sync_status.synced_at = datetime.now(timezone.utc)

        await db.commit()
        return len(records)
    except Exception:
        logger.exception("sync_from_source failed")
        raise


async def list_services(db: AsyncSession) -> list[PspFeeService]:
    try:
        result = await db.scalars(select(PspFeeService).order_by(PspFeeService.psp_rag_soc))
        _query_result_for_logging = list(result)
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("list_services query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("list_services failed")
        raise


async def get_sync_status(db: AsyncSession) -> PspFeeSyncStatus | None:
    try:
        _query_result_for_logging = await db.get(PspFeeSyncStatus, 1)
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("get_sync_status query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("get_sync_status failed")
        raise

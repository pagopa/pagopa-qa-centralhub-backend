# qa-hub-backend/app/services/psp_fee.py
from __future__ import annotations

from decimal import Decimal

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

# qa-hub-backend/tests/test_psp_fee_service.py
from __future__ import annotations

from decimal import Decimal

from app.services.psp_fee import normalize_record

SAMPLE_RECORD = {
    "psp_id": "12345",
    "psp_rag_soc": "Worldline Merchant Services Italia S.p.A.",
    "codice_abi": "03104",
    "nome_servizio": "Carta di debito",
    "descrizione_canale_mod_pag": "Pagamento con carta",
    "inf_desc_serv": "Pagamento con carta di debito",
    "inf_url_canale": "None",
    "url_informazioni_psp": "https://www.example.com",
    "importo_minimo": 0.01,
    "importo_massimo": 1500.0,
    "costo_fisso": 1.34,
    "canale_mod_pag_code": 5,
    "tipo_vers_cod": "CP",
    "canale_mod_pag": "WEB_PSP",
    "on_us": False,
    "carte": True,
    "conto": False,
    "altri_wisp": False,
    "altri_io": False,
    "is_duplicated": False,
    "conto_app": False,
    "carte_app": False,
}


def test_normalize_record_converts_none_string_to_null() -> None:
    record = normalize_record(SAMPLE_RECORD)
    assert record["inf_url_canale"] is None
    assert record["url_informazioni_psp"] == "https://www.example.com"


def test_normalize_record_converts_decimal_fields() -> None:
    record = normalize_record(SAMPLE_RECORD)
    assert record["importo_minimo"] == Decimal("0.01")
    assert record["importo_massimo"] == Decimal("1500.0")
    assert record["costo_fisso"] == Decimal("1.34")


def test_normalize_record_passes_through_booleans() -> None:
    record = normalize_record(SAMPLE_RECORD)
    assert record["carte"] is True
    assert record["on_us"] is False


def test_normalize_record_handles_null_decimal() -> None:
    raw = dict(SAMPLE_RECORD)
    raw["costo_fisso"] = None
    record = normalize_record(raw)
    assert record["costo_fisso"] is None

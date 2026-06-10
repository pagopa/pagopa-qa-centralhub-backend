from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.schemas.psp_fee import (
    PspFeeListResponse,
    PspFeeServiceOut,
    PspFeeSyncResponse,
    PspFeeSyncStatusOut,
)

SAMPLE_SERVICE = {
    "id": uuid.uuid4(),
    "psp_id": "12345",
    "psp_rag_soc": "Worldline Merchant Services Italia S.p.A.",
    "codice_abi": "03104",
    "nome_servizio": "Carta di debito",
    "descrizione_canale_mod_pag": "Pagamento con carta",
    "inf_desc_serv": "Pagamento con carta di debito",
    "inf_url_canale": None,
    "url_informazioni_psp": "https://www.example.com",
    "tipo_vers_cod": "CP",
    "canale_mod_pag": "WEB_PSP",
    "canale_mod_pag_code": 5,
    "importo_minimo": 0.01,
    "importo_massimo": 1500.0,
    "costo_fisso": 1.34,
    "on_us": False,
    "carte": True,
    "conto": False,
    "altri_wisp": False,
    "altri_io": False,
    "conto_app": False,
    "carte_app": False,
    "is_duplicated": False,
}


def test_psp_fee_service_out_from_dict() -> None:
    out = PspFeeServiceOut.model_validate(SAMPLE_SERVICE)
    assert out.psp_rag_soc == "Worldline Merchant Services Italia S.p.A."
    assert out.costo_fisso == 1.34
    assert out.inf_url_canale is None


def test_psp_fee_sync_status_out_from_dict() -> None:
    out = PspFeeSyncStatusOut.model_validate({
        "last_run": "20260610",
        "notebook_version": "0.4.0",
        "item_count": 370,
        "synced_at": datetime(2026, 6, 10, 8, 3, tzinfo=timezone.utc),
    })
    assert out.item_count == 370
    assert out.last_run == "20260610"


def test_psp_fee_list_response_with_null_sync_status() -> None:
    resp = PspFeeListResponse(items=[], sync_status=None)
    assert resp.items == []
    assert resp.sync_status is None


def test_psp_fee_sync_response() -> None:
    resp = PspFeeSyncResponse(status="ok", item_count=370)
    assert resp.status == "ok"
    assert resp.item_count == 370

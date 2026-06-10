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


import pytest
from unittest.mock import AsyncMock, patch

from app.models.psp_fee import PspFeeService, PspFeeSyncStatus
from app.services.psp_fee import (
    fetch_catalog,
    get_sync_status,
    list_services,
    sync_from_source,
)


@pytest.mark.anyio
async def test_fetch_catalog_calls_configured_url() -> None:
    mock_response = AsyncMock()
    mock_response.raise_for_status = lambda: None
    mock_response.json = lambda: {"content": []}

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response) as mock_get:
        result = await fetch_catalog()

    mock_get.assert_called_once()
    assert result == {"content": []}


@pytest.mark.anyio
async def test_sync_from_source_empty_content_raises() -> None:
    db = AsyncMock()

    with patch(
        "app.services.psp_fee.fetch_catalog",
        new_callable=AsyncMock,
        return_value={"content": []},
    ):
        with pytest.raises(ValueError):
            await sync_from_source(db)

    db.execute.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.anyio
async def test_sync_from_source_replaces_and_commits() -> None:
    db = AsyncMock()
    db.get.return_value = None

    catalog = {
        "last_Run": "20260610",
        "notebookVersion": "0.4.0",
        "content": [SAMPLE_RECORD, SAMPLE_RECORD],
    }

    with patch(
        "app.services.psp_fee.fetch_catalog",
        new_callable=AsyncMock,
        return_value=catalog,
    ):
        count = await sync_from_source(db)

    assert count == 2
    db.execute.assert_called_once()
    db.add_all.assert_called_once()
    inserted = db.add_all.call_args[0][0]
    assert len(inserted) == 2
    assert all(isinstance(r, PspFeeService) for r in inserted)
    db.add.assert_called_once()
    added_status = db.add.call_args[0][0]
    assert isinstance(added_status, PspFeeSyncStatus)
    assert added_status.last_run == "20260610"
    assert added_status.notebook_version == "0.4.0"
    assert added_status.item_count == 2
    db.commit.assert_called_once()


@pytest.mark.anyio
async def test_list_services_returns_scalars() -> None:
    db = AsyncMock()
    db.scalars.return_value = [PspFeeService(psp_rag_soc="A"), PspFeeService(psp_rag_soc="B")]

    result = await list_services(db)

    assert len(result) == 2


@pytest.mark.anyio
async def test_get_sync_status_returns_none_when_absent() -> None:
    db = AsyncMock()
    db.get.return_value = None

    result = await get_sync_status(db)

    assert result is None

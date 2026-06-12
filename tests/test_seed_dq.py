from __future__ import annotations

import csv
import io
import logging

from app.models.dq import DqCategory, DqRiskLevel
from scripts.seed_dq import normalize_header, read_catalog_csv, read_instance_csv


def _write_csv(tmp_path, name: str, content: str):
    path = tmp_path / name
    path.write_bytes(content.encode("utf-8-sig"))
    return path


def test_normalize_header_strips_bom_and_aliases() -> None:
    assert normalize_header("﻿Tipo Controllo") == "Tipo controllo"
    assert normalize_header("Tabelle") == "Tabella"
    assert normalize_header("Campi") == "Campo"
    assert normalize_header("Owner") == "Owner"


def test_read_catalog_csv_parses_rows(tmp_path) -> None:
    content = (
        "Nome Controllo,Descrizione,Dimensione DQ\n"
        "Check not null,Verifica campo required,Validità\n"
    )
    path = _write_csv(tmp_path, "Controlli Puntuali.csv", content)

    rows = read_catalog_csv(path, DqCategory.PUNTUALE)

    assert rows == [
        {
            "category": DqCategory.PUNTUALE,
            "name": "Check not null",
            "description": "Verifica campo required",
            "dimension_name": "Validità",
        }
    ]


def test_read_catalog_csv_skips_rows_without_dimension(tmp_path) -> None:
    content = (
        "Nome Controllo,Descrizione,Dimensione DQ\n"
        "Check not null,Verifica campo required,Validità\n"
        ",,\n"
    )
    path = _write_csv(tmp_path, "Controlli Puntuali.csv", content)

    rows = read_catalog_csv(path, DqCategory.PUNTUALE)

    assert len(rows) == 1


def test_read_instance_csv_parses_rows(tmp_path) -> None:
    content = (
        "Tipo controllo,Tabella,Campo,Owner,Rischio,Impatto,Note,Stato\n"
        "Check not null,pagopa.bronze_gpd_payment_position,after.id,,BASSO,ALTO,Verifica campo required,\n"
    )
    path = _write_csv(tmp_path, "GPD - Controlli Puntuali.csv", content)

    rows = read_instance_csv(path, "GPD", DqCategory.PUNTUALE)

    assert rows == [
        {
            "domain_name": "GPD",
            "category": DqCategory.PUNTUALE,
            "control_type": "Check not null",
            "table_ref": "pagopa.bronze_gpd_payment_position",
            "field_ref": "after.id",
            "owner": None,
            "risk": DqRiskLevel.BASSO,
            "impact": DqRiskLevel.ALTO,
            "notes": "Verifica campo required",
        }
    ]


def test_read_instance_csv_normalizes_header_variants(tmp_path) -> None:
    content = (
        "Tipo Controllo,Tabelle,Campi,Owner,Rischio,Impatto,Note,Stato\n"
        "Check not null,pagopa.bronze_gec_table,after.id,,ALTO,MEDIO,,\n"
    )
    path = _write_csv(tmp_path, "GEC - Controlli Puntuali.csv", content)

    rows = read_instance_csv(path, "GEC", DqCategory.PUNTUALE)

    assert rows[0]["table_ref"] == "pagopa.bronze_gec_table"
    assert rows[0]["risk"] == DqRiskLevel.ALTO
    assert rows[0]["impact"] == DqRiskLevel.MEDIO


def test_read_instance_csv_skips_rows_with_empty_control_type(tmp_path, caplog) -> None:
    content = (
        "Tipo controllo,Tabella,Campo,Owner,Rischio,Impatto,Note,Stato\n"
        "Check not null,pagopa.bronze_table,after.id,,BASSO,ALTO,,\n"
        ",pagopa.bronze_table,after.other_field,,BASSO,ALTO,,\n"
    )
    path = _write_csv(tmp_path, "Wallet - Controlli Intra-entità.csv", content)

    with caplog.at_level(logging.WARNING):
        rows = read_instance_csv(path, "Wallet", DqCategory.INTRA_ENTITA)

    assert len(rows) == 1
    assert "Skipping" in caplog.text


def test_read_instance_csv_handles_empty_file(tmp_path) -> None:
    content = "Tipo controllo,Tabella,Campo,Owner,Rischio,Impatto,Note,Stato\n"
    path = _write_csv(tmp_path, "BIZ - Controlli Puntuali.csv", content)

    rows = read_instance_csv(path, "BIZ", DqCategory.PUNTUALE)

    assert rows == []

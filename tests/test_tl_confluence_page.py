# ruff: noqa: S101
"""Fase 5, Task 4: lettura delle pagine FEAT di Confluence."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models.test_lifecycle import TlFeatureStatus
from app.services.test_lifecycle.confluence_page import map_status, parse_feat_page

FIX = Path(__file__).parent / "fixtures" / "confluence"


def _page(name):
    return parse_feat_page((FIX / name).read_text(encoding="utf-8"))


def test_wip_page_fields_and_scenarios():
    p = _page("feat_wip.xml")
    assert (p.code, p.doc_name, p.area, p.status_text) == (
        "FEAT_007_FdR3", "FEAT_007_FdR3_Conversione flussi", "FdR", "WIP"
    )
    assert p.owner_accounts == ["acc-owner"]
    assert p.reviewer_accounts == ["acc-owner", "acc-2"] and p.reviewer_label == "qa_team"
    assert (p.jira_design, p.product_key, p.jira_impl) == ("PQ-566", "PQ-495", None)
    assert p.requirements_url is None and p.feature_file_url is None
    assert p.estimate_days is None
    assert p.description == "Conversione da JSON a XML di un flusso"
    assert p.background_steps == "Dati i sistemi sono operativi"
    first, second = p.scenarios
    assert (first.number, first.title) == (1, "Conversione da Json a XML")
    assert first.steps == (
        'Dato il PSP "PSPDEMO" censito\nE che il flusso "F1" è PUBLISHED\n'
        "Quando viene attivato il convertitore\n"
        'Allora su Fdr1 è presente il flusso "F1"'
    )
    assert second.steps == (
        'Dato il flusso "F1" è in errore\nE il PSP è censito\n'
        "Quando viene chiamato il retry singolo\nAllora il flusso è presente"
    )
    assert p.note == ""


def test_implemented_page():
    p = _page("feat_implemented.xml")
    assert p.status_text == "TEST SUITE IMPLEMENTED"
    assert (p.owner_accounts, p.reviewer_accounts, p.reviewer_label) == (["acc-9"], ["acc-8"], None)
    assert (p.jira_design, p.jira_impl, p.product_key) == ("PQ-120", "PQ-121", "CHK-77")
    assert p.requirements_url == "https://docs.example.test/srs"
    assert p.feature_file_url.endswith("/features/checkout_npg_authorization.feature")
    assert p.estimate_days == 2.5
    assert p.description == "Checkout eCommerce — Autorizzazione"
    assert p.scenarios == []


def test_irregular_page_never_raises():
    p = _page("feat_irregular.xml")
    assert p.code == "FEAT_002_CUP" and p.status_text == "Approvato Andrea"
    assert [s.number for s in p.scenarios] == [2]
    assert p.scenarios[0].steps == "Quando succede"
    assert "Scenario 01: Senza tabella" in p.note and "Testo libero" in p.note
    assert parse_feat_page("<p>niente</p>").code is None
    assert parse_feat_page("<table><tr><td>rotto").scenarios == []


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("test suite implemented", TlFeatureStatus.IMPLEMENTED),
        ("  TEST SUITE   IMPLEMENTED ", TlFeatureStatus.IMPLEMENTED),
        ("Approvato Andrea Ferracci", TlFeatureStatus.DESIGNED),
        ("WIP", TlFeatureStatus.DRAFT),
        ("IN PROGRESS", TlFeatureStatus.DRAFT),
        (None, TlFeatureStatus.DRAFT),
    ],
)
def test_map_status(text, status):
    assert map_status(text) == status

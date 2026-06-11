from __future__ import annotations

from app.services.gpd_position import parse_report_line

SAMPLE_REPORT = {
    "TOTAL": 275910881,
    "GPD": 110440874,
    "GPD_PAYABLE": 59543593,
    "WISP": 20017656,
    "GPD4ACA": 117079456,
    "GPD4ACA_PAYABLE": 57082760,
    "PA_CREATE_POSITION": 28372895,
    "PA_CREATE_POSITION_PAYABLE": 15027872,
}

SAMPLE_LOG = (
    "2026-06-09T03:14:02.1234567Z report data " + repr(SAMPLE_REPORT) + "\n"
    "2026-06-09T03:14:02.2345678Z creating json report\n"
    "2026-06-09T03:14:02.3456789Z json report created\n"
)


def test_parse_report_line_extracts_dict() -> None:
    result = parse_report_line(SAMPLE_LOG)
    assert result == SAMPLE_REPORT


def test_parse_report_line_returns_none_when_missing() -> None:
    assert parse_report_line("some unrelated log line\nanother line\n") is None


def test_parse_report_line_returns_none_on_invalid_dict() -> None:
    assert parse_report_line("report data {invalid python}\n") is None

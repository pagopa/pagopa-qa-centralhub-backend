from __future__ import annotations

# ruff: noqa: S101
import io
import json
import zipfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.services.sanp_health_artifacts import (
    MAX_ARTIFACT_BYTES,
    MAX_RESULT_JSON_BYTES,
    DriftArtifactError,
    DriftArtifactPayload,
    classify_result,
    parse_drift_artifact,
)

VALID_RESULT: dict[str, Any] = {
    "file": "gpd.json",
    "env": "DEV",
    "branch": "SANP3.13.0",
    "display_name": "GPD Payments pagoPA - REST for Auth",
    "description": "REST API del servizio Payments",
    "changes": [
        {
            "level": 3,
            "id": "oasdiff-rule",
            "text": "human-readable message",
            "path": "/positions",
            "operation": "GET",
            "section": None,
            "comment": None,
        }
    ],
}


def _zip_members(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _result_zip(result: dict[str, Any] = VALID_RESULT) -> bytes:
    return _zip_members({"result.json": json.dumps(result).encode()})


def test_parse_valid_artifact() -> None:
    result = parse_drift_artifact("drift-d-gpd", _result_zip())

    assert isinstance(result, DriftArtifactPayload)
    assert result.file == "gpd.json"
    assert result.env == "SANP"
    assert result.branch == "SANP3.13.0"
    assert result.changes[0].level == 3
    assert result.changes[0].id == "oasdiff-rule"


@pytest.mark.parametrize(
    ("source_env", "expected_env"),
    [("DEV", "SANP"), ("UAT", "COLLAUDO"), ("PROD", "PRODUZIONE")],
)
def test_parse_maps_environment(source_env: str, expected_env: str) -> None:
    payload = {**VALID_RESULT, "env": source_env}

    result = parse_drift_artifact("drift-report", _result_zip(payload))

    assert result is not None
    assert result.env == expected_env


def test_parse_rejects_malformed_zip() -> None:
    with pytest.raises(DriftArtifactError, match="ZIP"):
        parse_drift_artifact("drift-d-gpd", b"not a zip")


def test_parse_rejects_malformed_json() -> None:
    payload = _zip_members({"result.json": b"{not json"})

    with pytest.raises(DriftArtifactError, match="JSON"):
        parse_drift_artifact("drift-d-gpd", payload)


def test_parse_rejects_unknown_payload_fields() -> None:
    payload = {**VALID_RESULT, "unexpected": "value"}

    with pytest.raises(DriftArtifactError, match="JSON"):
        parse_drift_artifact("drift-d-gpd", _result_zip(payload))


def test_parse_rejects_html_in_artifact_payload() -> None:
    payload = {**VALID_RESULT, "html": "<p>rendered report</p>"}

    with pytest.raises(DriftArtifactError, match="JSON"):
        parse_drift_artifact("drift-d-gpd", _result_zip(payload))


@pytest.mark.parametrize(
    ("field", "limit", "change_field"),
    [
        ("file", 500, False),
        ("branch", 255, False),
        ("display_name", 500, False),
        ("id", 255, True),
        ("operation", 20, True),
        ("section", 255, True),
    ],
)
def test_parse_rejects_values_longer_than_database_columns(
    field: str,
    limit: int,
    change_field: bool,
) -> None:
    payload = {**VALID_RESULT, "changes": [{**VALID_RESULT["changes"][0]}]}
    if change_field:
        payload["changes"][0][field] = "x" * (limit + 1)
    else:
        payload[field] = "x" * (limit + 1)

    with pytest.raises(DriftArtifactError, match="JSON"):
        parse_drift_artifact("drift-d-gpd", _result_zip(payload))


def test_parse_normalizes_unsupported_zip_compression() -> None:
    with patch("zipfile.ZipFile", side_effect=NotImplementedError("unsupported")):
        with pytest.raises(DriftArtifactError, match="ZIP"):
            parse_drift_artifact("drift-d-gpd", b"zip")


def test_parse_reads_result_json_with_a_hard_limit() -> None:
    member = MagicMock()
    member.is_dir.return_value = False
    member.filename = "result.json"
    member.file_size = 1
    stream = MagicMock()
    stream.read.return_value = b"x" * (MAX_RESULT_JSON_BYTES + 1)
    archive = MagicMock()
    archive.__enter__.return_value = archive
    archive.infolist.return_value = [member]
    archive.open.return_value.__enter__.return_value = stream

    with patch("zipfile.ZipFile", return_value=archive):
        with pytest.raises(DriftArtifactError, match="result.json.*size"):
            parse_drift_artifact("drift-d-gpd", b"zip")

    stream.read.assert_called_once_with(MAX_RESULT_JSON_BYTES + 1)


def test_parse_rejects_artifact_above_limit() -> None:
    with pytest.raises(DriftArtifactError, match="artifact.*size"):
        parse_drift_artifact("drift-d-gpd", b"x" * (MAX_ARTIFACT_BYTES + 1))


def test_parse_rejects_result_json_above_limit() -> None:
    oversized_json = b" " * (MAX_RESULT_JSON_BYTES + 1)
    payload = _zip_members({"result.json": oversized_json})

    with pytest.raises(DriftArtifactError, match="result.json.*size"):
        parse_drift_artifact("drift-d-gpd", payload)


def test_parse_rejects_missing_result_json() -> None:
    payload = _zip_members({"other.json": b"{}"})

    with pytest.raises(DriftArtifactError, match="result.json"):
        parse_drift_artifact("drift-d-gpd", payload)


def test_parse_rejects_nested_result_json() -> None:
    payload = _zip_members({"nested/result.json": json.dumps(VALID_RESULT).encode()})

    with pytest.raises(DriftArtifactError, match="root"):
        parse_drift_artifact("drift-d-gpd", payload)


def test_parse_rejects_extra_file() -> None:
    payload = _zip_members(
        {
            "result.json": json.dumps(VALID_RESULT).encode(),
            "notes.txt": b"unexpected",
        }
    )

    with pytest.raises(DriftArtifactError, match="exactly one"):
        parse_drift_artifact("drift-d-gpd", payload)


def test_parse_rejects_unknown_environment() -> None:
    payload = {**VALID_RESULT, "env": "QA"}

    with pytest.raises(DriftArtifactError, match="environment"):
        parse_drift_artifact("drift-q-gpd", _result_zip(payload))


@pytest.mark.parametrize(
    ("artifact_name", "payload_env"),
    [
        ("drift-d-gpd", "UAT"),
        ("drift-u-gpd", "PROD"),
        ("drift-p-gpd", "DEV"),
    ],
)
def test_parse_rejects_artifact_prefix_environment_mismatch(
    artifact_name: str,
    payload_env: str,
) -> None:
    payload = {**VALID_RESULT, "env": payload_env}

    with pytest.raises(DriftArtifactError, match="prefix.*environment"):
        parse_drift_artifact(artifact_name, _result_zip(payload))


def test_parse_skips_drift_main_artifact() -> None:
    assert parse_drift_artifact("drift-main-gpd", b"not even a zip") is None


def test_parse_skips_main_payload() -> None:
    payload = {**VALID_RESULT, "env": "MAIN"}

    assert parse_drift_artifact("drift-d-gpd", _result_zip(payload)) is None


@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        ([3, 2, 1], "KO"),
        ([2, 1], "WARNING"),
        ([1], "INFO"),
        ([], "OK"),
    ],
)
def test_classify_result(levels: list[int], expected: str) -> None:
    changes = [
        {
            "level": level,
            "id": f"rule-{level}",
            "text": "change",
            "path": None,
            "operation": None,
            "section": None,
            "comment": None,
        }
        for level in levels
    ]
    payload = DriftArtifactPayload.model_validate({**VALID_RESULT, "changes": changes})

    assert classify_result(payload.changes) == expected


@pytest.mark.parametrize(
    ("level", "severity"),
    [(3, "error"), (2, "warning"), (1, "info")],
)
def test_change_maps_level_to_severity(level: int, severity: str) -> None:
    change = {
        "level": level,
        "id": "rule",
        "text": "change",
        "path": None,
        "operation": None,
        "section": None,
        "comment": None,
    }

    payload = DriftArtifactPayload.model_validate({**VALID_RESULT, "changes": [change]})

    assert payload.changes[0].severity == severity

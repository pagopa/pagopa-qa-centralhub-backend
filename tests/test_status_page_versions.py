from __future__ import annotations

# ruff: noqa: S101
import pytest

from app.services.status_page_versions import Drift, compare_versions, drift, normalize_version


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("1.4.2", "1.4.2", 0),
        ("v1.4.2", "1.4.2", 0),
        ("Release 1.4.2", "1.4.2", 0),
        ("2.0.0", "1.9.9", 1),
        ("1.9.9", "2.0.0", -1),
        ("1.10.0", "1.9.0", 1),
        ("1.4.10", "1.4.9", 1),
        ("1.4.3-1-feature", "1.4.3", -1),
        ("1.4.3-1-feature", "1.4.2", 1),
        ("1.0.0-alpha", "1.0.0-alpha.1", -1),
        ("1.0.0-alpha.1", "1.0.0-beta", -1),
        ("1.0.0+build.5", "1.0.0", 0),
        ("1.0.0-1", "1.0.0-alpha", -1),
        ("1.0.0-2", "1.0.0-10", -1),
        ("latest", "1.0.0", None),
        ("1.0", "1.0.0", None),
        ("", "1.0.0", None),
        ("1.0.0", "", None),
    ],
)
def test_compare_versions(a: str, b: str, expected: int | None) -> None:
    assert compare_versions(a, b) == expected


def test_normalize_version() -> None:
    assert normalize_version("  Release v1.2.3 ") == "1.2.3"
    assert normalize_version("V1.2.3") == "1.2.3"


def test_drift() -> None:
    assert drift("1.4.1", "1.4.2") is Drift.BEHIND
    assert drift("1.4.3-1-feature", "1.4.2") is Drift.AHEAD
    assert drift("1.4.2", "v1.4.2") is Drift.EQUAL
    assert drift(None, "1.4.2") is Drift.UNKNOWN
    assert drift("abc", "1.4.2") is Drift.UNKNOWN
    assert drift("1.4.2", None) is Drift.UNKNOWN

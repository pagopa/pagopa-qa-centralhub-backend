# ruff: noqa: S101
"""Tag vocabulary and rules (design 2026-11-26, tag catalog)."""
from __future__ import annotations

import pytest

from app.services.test_lifecycle import tags


def test_vocabulary():
    assert tags.TEST_TYPES == ("integration", "e2e", "api")
    assert tags.STATUS_TAGS == ("runnable", "skip", "broken")
    assert tags.FLAKY_TAG == "flaky"
    assert tags.POLARITY_TAGS == ("positive", "negative")
    assert tags.LEGACY_POLARITY_TAGS == ("positivo", "negativo")


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (["runnable", "skip"], ["skip"]),
        (["broken", "runnable"], ["broken"]),
        (["skip", "broken"], ["skip"]),
        ([], ["runnable"]),
        (["smoke", "positive"], ["smoke", "runnable"]),
        (["smoke", "runnable", "negative", "flaky"], ["smoke", "runnable", "flaky"]),
        (["a", "a", "runnable"], ["a", "runnable"]),
        (["positivo", "smoke", "negativo"], ["smoke", "runnable"]),
    ],
)
def test_normalize_status(given, expected):
    assert tags.normalize_status(given) == expected


def test_normalize_status_is_idempotent():
    once = tags.normalize_status(["x", "runnable", "skip", "positive"])
    assert tags.normalize_status(once) == once


def test_normalize_status_does_not_mutate_input():
    given = ["runnable", "skip"]
    tags.normalize_status(given)
    assert given == ["runnable", "skip"]


@pytest.mark.parametrize(
    ("given", "count"),
    [([], 0), (["smoke"], 0), (["runnable"], 1), (["runnable", "skip"], 2), (["skip", "skip"], 1)],
)
def test_status_count(given, count):
    assert tags.status_count(given) == count


def test_legacy_test_type_tags():
    assert tags.legacy_test_type_tags(["e2e", "smoke", "api"]) == ["e2e", "api"]
    assert tags.legacy_test_type_tags(["ui", "runnable"]) == []


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("src/integration/fdr", "integration"),
        ("/src/e2e/", "e2e"),
        ("src/api", "api"),
        ("src/api/", "api"),
        ("docs", None),
        ("src/integrationx", None),
        ("", None),
        (None, None),
    ],
)
def test_default_test_type(folder, expected):
    assert tags.default_test_type(folder) == expected


def test_reason_texts_are_exact():
    assert tags.TEST_TYPE_MISSING_REASON == "Manca il test-type del file"
    assert tags.status_reason("@POS_006_01") == (
        "Lo scenario @POS_006_01 deve avere uno e un solo tag tra @runnable, @skip, @broken"
    )

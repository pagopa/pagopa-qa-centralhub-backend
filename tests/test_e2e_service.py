from __future__ import annotations

from datetime import datetime, timezone

from app.services.e2e import build_allure_url, derive_status, duration_to_ms, parse_run_at


def test_derive_status_passed() -> None:
    assert derive_status(passed=47, failed=0) == "passed"


def test_derive_status_failed() -> None:
    assert derive_status(passed=31, failed=7) == "failed"


def test_derive_status_mixed_all_zero() -> None:
    assert derive_status(passed=0, failed=0) == "mixed"


def test_duration_to_ms_basic() -> None:
    assert duration_to_ms("00:04:12") == 252_000


def test_duration_to_ms_with_hours() -> None:
    assert duration_to_ms("01:00:00") == 3_600_000


def test_parse_run_at_returns_utc() -> None:
    dt = parse_run_at("2026-05-26_06:32:00")
    assert dt == datetime(2026, 5, 26, 6, 32, 0, tzinfo=timezone.utc)


def test_build_allure_url() -> None:
    url = build_allure_url(
        github_repo="pagopa/pagopa-platform-integration-test",
        suite_path="wisp-tests",
        timestamp="2026-05-26_06:32:00",
    )
    assert url == (
        "https://pagopa.github.io/pagopa-platform-integration-test"
        "/wisp-tests/2026-05-26_06:32:00/index.html"
    )

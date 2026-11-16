from __future__ import annotations

# ruff: noqa: S101
from app.config import Settings
from app.core.features import FEATURE_CATALOG


def test_release_status_feature_registered() -> None:
    f = next(f for f in FEATURE_CATALOG if f["key"] == "release_status")
    assert (f["route"], f["action"], f["label"], f["external"]) == (
        "/releases",
        "view:status_page",
        "Release Status",
        False,
    )


def test_status_page_settings_defaults() -> None:
    s = Settings()
    assert s.status_page_dev_base_url == "https://api.dev.platform.pagopa.it"
    assert s.status_page_uat_base_url == "https://api.uat.platform.pagopa.it"
    assert s.status_page_prod_base_url == "https://api.platform.pagopa.it"
    assert s.status_page_info_path == "/shared/statuspage/v1/info"
    assert s.status_page_sync_concurrency == 10
    assert s.status_page_http_timeout_s == 10.0
    assert s.status_page_sync_interval_s == 900.0

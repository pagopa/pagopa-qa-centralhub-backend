from __future__ import annotations

from datetime import date, datetime, timezone

from app.schemas.gpd_position import (
    GpdPositionSnapshotOut,
    GpdPositionsResponse,
    GpdPositionSyncResponse,
    GpdPositionSyncStatusOut,
)

SAMPLE_SNAPSHOT = {
    "report_date": date(2026, 6, 9),
    "total": 275910881,
    "gpd": 110440874,
    "gpd_payable": 59543593,
    "gpd4aca": 117079456,
    "gpd4aca_payable": 57082760,
    "wisp": 20017656,
    "pa_create_position": 28372895,
    "pa_create_position_payable": 15027872,
}


def test_gpd_position_snapshot_out_from_dict() -> None:
    out = GpdPositionSnapshotOut.model_validate(SAMPLE_SNAPSHOT)
    assert out.report_date == date(2026, 6, 9)
    assert out.total == 275910881
    assert out.pa_create_position_payable == 15027872


def test_gpd_position_sync_status_out_from_dict() -> None:
    out = GpdPositionSyncStatusOut.model_validate({
        "item_count": 90,
        "synced_at": datetime(2026, 6, 11, 4, 0, tzinfo=timezone.utc),
    })
    assert out.item_count == 90


def test_gpd_positions_response_with_null_sync_status() -> None:
    resp = GpdPositionsResponse(items=[], sync_status=None)
    assert resp.items == []
    assert resp.sync_status is None


def test_gpd_position_sync_response() -> None:
    resp = GpdPositionSyncResponse(status="ok", item_count=1)
    assert resp.status == "ok"
    assert resp.item_count == 1

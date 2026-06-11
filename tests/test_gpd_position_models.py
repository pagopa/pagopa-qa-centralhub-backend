from __future__ import annotations

from app.models.gpd_position import GpdPositionSnapshot, GpdPositionSyncStatus


def test_gpd_position_snapshot_tablename() -> None:
    assert GpdPositionSnapshot.__tablename__ == "gpd_position_snapshots"


def test_gpd_position_sync_status_tablename() -> None:
    assert GpdPositionSyncStatus.__tablename__ == "gpd_position_sync_status"

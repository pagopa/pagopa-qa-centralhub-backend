from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class GpdPositionSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    report_date: date
    total: int
    gpd: int
    gpd_payable: int
    gpd4aca: int
    gpd4aca_payable: int
    wisp: int
    pa_create_position: int
    pa_create_position_payable: int


class GpdPositionSyncStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_count: int
    synced_at: datetime


class GpdPositionsResponse(BaseModel):
    items: list[GpdPositionSnapshotOut]
    sync_status: GpdPositionSyncStatusOut | None


class GpdPositionSyncResponse(BaseModel):
    status: str
    item_count: int

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, status

from app.deps import DbDep
from app.schemas.gpd_position import GpdPositionsResponse, GpdPositionSyncResponse
from app.services import gpd_position as gpd_position_svc

router = APIRouter()


@router.get("/snapshots", response_model=GpdPositionsResponse)
async def list_snapshots(db: DbDep) -> GpdPositionsResponse:
    items = await gpd_position_svc.list_snapshots(db)
    sync_status = await gpd_position_svc.get_sync_status(db)
    return GpdPositionsResponse(items=items, sync_status=sync_status)


@router.post("/sync", response_model=GpdPositionSyncResponse)
async def trigger_sync(db: DbDep) -> GpdPositionSyncResponse:
    try:
        count = await gpd_position_svc.sync_from_source(db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return GpdPositionSyncResponse(status="ok", item_count=count)

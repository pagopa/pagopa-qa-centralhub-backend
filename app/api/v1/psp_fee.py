# qa-hub-backend/app/api/v1/psp_fee.py
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, status

from app.deps import DbDep
from app.schemas.psp_fee import PspFeeListResponse, PspFeeSyncResponse
from app.services import psp_fee as psp_fee_svc

router = APIRouter()


@router.get("", response_model=PspFeeListResponse)
async def list_psp_fees(db: DbDep) -> PspFeeListResponse:
    items = await psp_fee_svc.list_services(db)
    sync_status = await psp_fee_svc.get_sync_status(db)
    return PspFeeListResponse(items=items, sync_status=sync_status)


@router.post("/sync", response_model=PspFeeSyncResponse)
async def trigger_sync(db: DbDep) -> PspFeeSyncResponse:
    try:
        count = await psp_fee_svc.sync_from_source(db)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return PspFeeSyncResponse(status="ok", item_count=count)

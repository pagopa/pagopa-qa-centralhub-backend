from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from app.services import metrics as metrics_svc


router = APIRouter()

@router.get("/guest-token")
async def get_guest_token() -> str:
    guest_token =  await metrics_svc.obtain_guest_token()
    if guest_token == '':
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error obtaining guest token"
        )
    return guest_token



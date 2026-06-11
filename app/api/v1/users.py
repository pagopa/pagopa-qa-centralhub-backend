from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from app.deps import DbDep
from app.schemas.user import (
    SyncLoginRequest,
    SyncLoginResponse,
    UserListResponse,
    UserOut,
    UserUpdate,
)
from app.services import users as users_svc

router = APIRouter()


@router.get("", response_model=UserListResponse)
async def list_users(db: DbDep) -> UserListResponse:
    items = await users_svc.list_users(db)
    return UserListResponse(items=[UserOut.model_validate(u) for u in items])


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: uuid.UUID, payload: UserUpdate, db: DbDep) -> UserOut:
    try:
        user = await users_svc.update_user(db, user_id, payload.role, payload.is_active)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return UserOut.model_validate(user)


@router.post("/sync-login", response_model=SyncLoginResponse)
async def sync_login(payload: SyncLoginRequest, db: DbDep) -> SyncLoginResponse:
    user = await users_svc.sync_login(db, payload.email, payload.name, payload.idp_sub)
    return SyncLoginResponse(role=user.role, is_active=user.is_active)

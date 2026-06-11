from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class UserListResponse(BaseModel):
    items: list[UserOut]


class UserUpdate(BaseModel):
    role: str | None = None
    is_active: bool | None = None


class SyncLoginRequest(BaseModel):
    email: str
    name: str
    idp_sub: str | None = None


class SyncLoginResponse(BaseModel):
    role: str
    is_active: bool

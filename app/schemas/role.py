from __future__ import annotations

from pydantic import BaseModel


class ActionCatalogEntry(BaseModel):
    key: str
    label: str
    category: str


class RoleOut(BaseModel):
    key: str
    label: str
    is_system: bool


class RoleMatrixResponse(BaseModel):
    roles: list[RoleOut]
    catalog: list[ActionCatalogEntry]
    matrix: dict[str, dict[str, bool]]


class RolePermissionUpdate(BaseModel):
    permissions: dict[str, bool]

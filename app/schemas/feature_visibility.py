from __future__ import annotations

from pydantic import BaseModel


class FeatureVisibilityItem(BaseModel):
    key: str
    label: str
    route: str
    action: str
    external: bool
    enabled: bool


class FeatureVisibilityResponse(BaseModel):
    items: list[FeatureVisibilityItem]


class FeatureVisibilityUpdate(BaseModel):
    enabled: bool


class FeatureVisibilityUpdateResponse(BaseModel):
    key: str
    enabled: bool
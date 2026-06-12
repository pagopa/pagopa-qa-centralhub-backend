from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


# ── Dimensions ────────────────────────────────────────────────────────────────

class DqDimensionBase(BaseModel):
    name: str
    sort_order: int = 0


class DqDimensionCreate(DqDimensionBase):
    pass


class DqDimensionUpdate(BaseModel):
    name: str | None = None
    sort_order: int | None = None


class DqDimensionOut(DqDimensionBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID


# ── Catalog controls ────────────────────────────────────────────────────────

class DqCatalogControlBase(BaseModel):
    category: str
    name: str
    description: str
    dimension_id: uuid.UUID


class DqCatalogControlCreate(DqCatalogControlBase):
    pass


class DqCatalogControlUpdate(BaseModel):
    category: str | None = None
    name: str | None = None
    description: str | None = None
    dimension_id: uuid.UUID | None = None


class DqCatalogControlOut(DqCatalogControlBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dimension: DqDimensionOut
    created_at: datetime
    updated_at: datetime


# ── Domains ───────────────────────────────────────────────────────────────────

class DqDomainOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    sort_order: int
    created_at: datetime
    updated_at: datetime


# ── Control instances ────────────────────────────────────────────────────────

class DqControlInstanceBase(BaseModel):
    domain_id: uuid.UUID
    catalog_control_id: uuid.UUID
    table_ref: str
    field_ref: str
    owner: str | None = None
    risk: str
    impact: str
    status: str = "da_implementare"
    notes: str | None = None


class DqControlInstanceCreate(DqControlInstanceBase):
    pass


class DqControlInstanceUpdate(BaseModel):
    table_ref: str | None = None
    field_ref: str | None = None
    owner: str | None = None
    risk: str | None = None
    impact: str | None = None
    status: str | None = None
    notes: str | None = None


class DqControlInstanceOut(DqControlInstanceBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    catalog_control: DqCatalogControlOut
    created_at: datetime
    updated_at: datetime

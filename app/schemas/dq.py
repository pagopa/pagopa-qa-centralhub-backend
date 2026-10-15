from __future__ import annotations

import uuid
import re
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


CATEGORY_PREFIXES = {
    "puntuale": "fld",
    "intra_entita": "ent",
    "cross_entita": "xref",
}
DIMENSION_CODES = {
    "Accuratezza": "acc",
    "Completezza": "cmp",
    "Consistenza": "cns",
    "Tempestività": "tim",
    "Unicità": "unq",
    "Validità": "vld",
}
DQ_CODE_PATTERN = re.compile(
    r"^(fld|ent|xref)__(acc|cmp|cns|tim|unq|vld)__[a-z0-9]+(?:_[a-z0-9]+)*__[a-z0-9]+(?:_[a-z0-9]+)*$"
)


def validate_dq_code(
    value: str, category: str | None = None, dimension: str | None = None
) -> str:
    if not DQ_CODE_PATTERN.fullmatch(value):
        raise ValueError("Use the format <category>__<dimension>__<context>__<rule> in lowercase snake_case")
    parts = value.split("__")
    category_value = getattr(category, "value", category)
    if category_value is not None and parts[0] != CATEGORY_PREFIXES.get(category_value):
        raise ValueError("The identifier prefix must match the selected control category")
    if dimension is not None and parts[1] != DIMENSION_CODES.get(dimension):
        raise ValueError("The identifier dimension must match the selected DQ dimension")
    return value


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
    created_by: str | None = None


class DqCatalogControlUpdate(BaseModel):
    category: str | None = None
    name: str | None = None
    description: str | None = None
    dimension_id: uuid.UUID | None = None
    updated_by: str | None = None


class DqCatalogControlOut(DqCatalogControlBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dimension: DqDimensionOut
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


# ── Domains ───────────────────────────────────────────────────────────────────

class DqDomainBase(BaseModel):
    name: str
    sort_order: int = 0


class DqDomainCreate(DqDomainBase):
    pass


class DqDomainUpdate(BaseModel):
    name: str | None = None
    sort_order: int | None = None


class DqDomainOut(DqDomainBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
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
    check_name: str | None = None
    soda_url: str | None = None

    @field_validator("check_name")
    @classmethod
    def check_name_uses_standard(cls, value: str | None) -> str | None:
        if value is not None:
            validate_dq_code(value)
        return value

    @field_validator("soda_url")
    @classmethod
    def soda_url_must_be_http(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("SODA link must be an absolute HTTP or HTTPS URL")
        return value


class DqControlInstanceCreate(DqControlInstanceBase):
    check_name: str
    created_by: str | None = None

    @model_validator(mode="after")
    def active_instance_requires_soda_url(self):
        if self.status == "attivo" and not self.soda_url:
            raise ValueError("An active control requires its SODA URL")
        return self


class DqControlInstanceUpdate(BaseModel):
    table_ref: str | None = None
    field_ref: str | None = None
    owner: str | None = None
    risk: str | None = None
    impact: str | None = None
    status: str | None = None
    notes: str | None = None
    check_name: str | None = None
    soda_url: str | None = None
    updated_by: str | None = None

    @field_validator("check_name")
    @classmethod
    def check_name_uses_standard(cls, value: str | None) -> str | None:
        if value is not None:
            validate_dq_code(value)
        return value

    @field_validator("soda_url")
    @classmethod
    def soda_url_must_be_http(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("SODA link must be an absolute HTTP or HTTPS URL")
        return value


class DqControlInstanceOut(DqControlInstanceBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    catalog_control: DqCatalogControlOut
    check_name: str | None
    soda_url: str | None
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime

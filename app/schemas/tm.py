from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field, model_validator


# ── ExternalResource ──────────────────────────────────────────────────────────

class ExternalResourceCreate(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    company: str = Field(..., min_length=1, max_length=200)
    role: str = Field(..., min_length=1, max_length=200)
    daily_rate: float = Field(..., gt=0)
    contract_start: date
    contract_end: date | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def check_dates(self) -> "ExternalResourceCreate":
        if self.contract_end and self.contract_end < self.contract_start:
            raise ValueError("contract_end must be >= contract_start")
        return self


class ExternalResourceUpdate(BaseModel):
    first_name: str | None = Field(None, min_length=1, max_length=100)
    last_name: str | None = Field(None, min_length=1, max_length=100)
    email: EmailStr | None = None
    company: str | None = Field(None, min_length=1, max_length=200)
    role: str | None = Field(None, min_length=1, max_length=200)
    daily_rate: float | None = Field(None, gt=0)
    contract_start: date | None = None
    contract_end: date | None = None
    notes: str | None = None
    is_active: bool | None = None


class ExternalResourceOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    first_name: str
    last_name: str
    email: str
    company: str
    role: str
    daily_rate: float
    contract_start: date
    contract_end: date | None
    notes: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ── ResourceAbsence ───────────────────────────────────────────────────────────

class ResourceAbsenceCreate(BaseModel):
    resource_id: uuid.UUID
    absence_date: date
    absence_type: str = "ferie"  # ferie | malattia | permesso | altro
    note: str | None = None


class ResourceAbsenceOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    resource_id: uuid.UUID
    absence_date: date
    absence_type: str
    source: str
    confluence_event_id: str | None
    note: str | None
    created_at: datetime


# ── Cost Report ───────────────────────────────────────────────────────────────

class ResourceCostRow(BaseModel):
    resource_id: uuid.UUID
    full_name: str
    company: str
    role: str
    working_days: int
    absence_days: int
    billable_days: int
    daily_rate: float
    total_cost: float


class CostReport(BaseModel):
    year: int
    month: int
    rows: list[ResourceCostRow]
    grand_total: float


# ── Confluence sync ───────────────────────────────────────────────────────────

class SyncResult(BaseModel):
    synced: int
    errors: list[str]


# ── CSV import ───────────────────────────────────────────────────────────────

class CsvAbsenceRow(BaseModel):
    email: EmailStr
    absence_date: date
    absence_type: str = "ferie"
    note: str | None = None


class CsvAbsenceImportRequest(BaseModel):
    rows: list[CsvAbsenceRow]


class CsvAbsenceImportResult(BaseModel):
    imported: int
    skipped: int
    errors: list[str]

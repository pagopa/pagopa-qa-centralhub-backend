from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

Severity = Literal["info", "warning", "critical"]


class InboxItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    body: str
    link: str | None
    severity: Severity
    type: str
    created_at: datetime
    read_at: datetime | None
    auto_read: bool


class InboxPageOut(BaseModel):
    items: list[InboxItemOut]
    next_cursor: str | None


class InboxSummaryOut(BaseModel):
    unread: int
    total: int
    max_unread_severity: Severity | None


class MarkReadOut(BaseModel):
    updated: int


class EventIngestOut(BaseModel):
    id: str
    status: Literal["accepted", "duplicate", "suppressed"]

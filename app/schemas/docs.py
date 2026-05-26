from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, HttpUrl


DocIcon = Literal["confluence", "page", "template", "web", "video"]
DocType = Literal["external", "embedded"]


class DocItemCreate(BaseModel):
    title: str
    description: str | None = None
    url: str
    type: DocType = "external"
    category: str = "Generale"
    icon: DocIcon = "page"
    thumbnail_url: str | None = None
    position: int = 0


class DocItemUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    url: str | None = None
    type: DocType | None = None
    category: str | None = None
    icon: DocIcon | None = None
    thumbnail_url: str | None = None
    position: int | None = None


class DocItemOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    url: str
    type: str
    category: str
    icon: str
    thumbnail_url: str | None
    position: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

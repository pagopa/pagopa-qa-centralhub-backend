from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class DocItem(Base):
    __tablename__ = "doc_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="external")
    category: Mapped[str] = mapped_column(String(100), nullable=False, server_default="Generale")
    icon: Mapped[str] = mapped_column(String(50), nullable=False, server_default="page")
    thumbnail_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

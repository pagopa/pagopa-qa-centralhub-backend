from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class EaAssistantSettings(Base):
    __tablename__ = "ea_assistant_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_ea_assistant_settings_singleton"),
        CheckConstraint("provider IN ('gemini', 'groq')", name="ck_ea_assistant_provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    gemini_model: Mapped[str] = mapped_column(String(100), nullable=False)
    groq_model: Mapped[str] = mapped_column(String(100), nullable=False)
    fallback_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

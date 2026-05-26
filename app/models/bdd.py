from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class BddProject(Base):
    __tablename__ = "bdd_projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    scenarios: Mapped[list[BddScenario]] = relationship(
        "BddScenario", back_populates="project", cascade="all, delete-orphan"
    )


class BddScenario(Base):
    __tablename__ = "bdd_scenarios"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bdd_projects.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    requirement: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    gherkin: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft")
    ai_provider: Mapped[str] = mapped_column(String(20), nullable=False)
    ai_model: Mapped[str] = mapped_column(String(100), nullable=False)
    generation_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    project: Mapped[BddProject] = relationship("BddProject", back_populates="scenarios")


class BddSettings(Base):
    __tablename__ = "bdd_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    ai_provider: Mapped[str] = mapped_column(String(20), nullable=False, server_default="ollama")
    claude_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    claude_model: Mapped[str] = mapped_column(String(100), nullable=False, server_default="claude-sonnet-4-6")
    ollama_base_url: Mapped[str] = mapped_column(String(500), nullable=False, server_default="http://localhost:11434")
    ollama_model: Mapped[str] = mapped_column(String(100), nullable=False, server_default="llama3.2")
    confluence_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    confluence_api_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    gherkin_language: Mapped[str] = mapped_column(String(5), nullable=False, server_default="it")
    max_scenarios: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __init__(
        self,
        id: int = 1,
        ai_provider: str = "ollama",
        claude_api_key: str | None = None,
        claude_model: str = "claude-sonnet-4-6",
        ollama_base_url: str = "http://localhost:11434",
        ollama_model: str = "llama3.2",
        confluence_email: str | None = None,
        confluence_api_token: str | None = None,
        gherkin_language: str = "it",
        max_scenarios: int = 5,
    ):
        self.id = id
        self.ai_provider = ai_provider
        self.claude_api_key = claude_api_key
        self.claude_model = claude_model
        self.ollama_base_url = ollama_base_url
        self.ollama_model = ollama_model
        self.confluence_email = confluence_email
        self.confluence_api_token = confluence_api_token
        self.gherkin_language = gherkin_language
        self.max_scenarios = max_scenarios

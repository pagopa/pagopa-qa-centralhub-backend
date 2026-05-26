from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    scenario_count: int = 0


class ScenarioCreate(BaseModel):
    project_id: uuid.UUID
    title: str
    requirement: str
    source_type: str
    source_ref: str | None = None
    gherkin: str
    tags: list[str] = []
    status: str = "draft"
    ai_provider: str
    ai_model: str
    generation_time_ms: int | None = None


class ScenarioUpdate(BaseModel):
    title: str | None = None
    gherkin: str | None = None
    tags: list[str] | None = None
    status: str | None = None


class ScenarioOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    requirement: str
    source_type: str
    source_ref: str | None
    gherkin: str
    tags: list[str]
    status: str
    ai_provider: str
    ai_model: str
    generation_time_ms: int | None
    created_at: datetime
    updated_at: datetime


class SettingsOut(BaseModel):
    ai_provider: str
    claude_api_key_set: bool
    claude_model: str
    ollama_base_url: str
    ollama_model: str
    confluence_email: str | None
    confluence_token_set: bool
    gherkin_language: str
    max_scenarios: int


class SettingsUpdate(BaseModel):
    ai_provider: str | None = None
    claude_api_key: str | None = None
    claude_model: str | None = None
    ollama_base_url: str | None = None
    ollama_model: str | None = None
    confluence_email: str | None = None
    confluence_api_token: str | None = None
    gherkin_language: str | None = None
    max_scenarios: int | None = None


class ParseRequest(BaseModel):
    source_type: str  # text | url | confluence
    content: str | None = None
    url: str | None = None
    confluence_page_id: str | None = None


class ParseResponse(BaseModel):
    text: str


class GenerateRequest(BaseModel):
    requirement: str
    title: str
    language: str = "it"
    max_scenarios: int = 5

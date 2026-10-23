from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

Provider = Literal["gemini", "groq"]
ModelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class AssistantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Provider = "gemini"
    gemini_model: ModelName = "gemini-3.8-flash"
    groq_model: ModelName = "openai/gpt-oss-120b"
    fallback_enabled: bool = False


class AssistantSettingsOut(AssistantConfig):
    configured: dict[Provider, bool]

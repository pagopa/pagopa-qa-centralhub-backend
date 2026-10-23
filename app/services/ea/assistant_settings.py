from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.ea_assistant import EaAssistantSettings
from app.schemas.ea_assistant import AssistantConfig, AssistantSettingsOut, Provider
from app.services.ea import assistant
from app.services.ea.graph_index import GraphIndex
from app.services.ea.groq import GroqModel
from app.services.ea.release_impact import ChangeSource

logger = structlog.get_logger(__name__)


async def get_config(db: AsyncSession) -> AssistantConfig:
    row = await db.get(EaAssistantSettings, 1)
    if row is None:
        return AssistantConfig.model_validate(
            {
                "provider": settings.ea_assistant_provider,
                "gemini_model": settings.gemini_model,
                "groq_model": settings.groq_model,
            }
        )
    return AssistantConfig.model_validate(
        {
            "provider": row.provider,
            "gemini_model": row.gemini_model,
            "groq_model": row.groq_model,
            "fallback_enabled": row.fallback_enabled,
        }
    )


def public_settings(config: AssistantConfig) -> AssistantSettingsOut:
    return AssistantSettingsOut(
        **config.model_dump(),
        configured={"gemini": bool(settings.gemini_api_key), "groq": bool(settings.groq_api_key)},
    )


async def save_config(db: AsyncSession, config: AssistantConfig) -> AssistantConfig:
    enabled = [config.provider]
    if config.fallback_enabled:
        enabled.append("groq" if config.provider == "gemini" else "gemini")
    configured = public_settings(config).configured
    for provider in enabled:
        if not configured[provider]:
            raise ValueError(f"Manca {provider.upper()}_API_KEY nei secret del backend")
    values = config.model_dump()
    statement = insert(EaAssistantSettings).values(id=1, **values)
    await db.execute(statement.on_conflict_do_update(index_elements=["id"], set_=values))
    await db.commit()
    logger.info("ea_assistant_settings_updated", **values)
    return config


def create_model(provider: Provider, config: AssistantConfig) -> assistant.ChatModel:
    if provider == "groq":
        return GroqModel(settings.groq_api_key, config.groq_model)
    fallbacks = [m.strip() for m in settings.gemini_fallback_models.split(",") if m.strip()]
    return assistant.GeminiModel(settings.gemini_api_key, config.gemini_model, fallbacks)


async def answer(
    graph: GraphIndex,
    config: AssistantConfig,
    messages: list[dict[str, str]],
    changes: ChangeSource | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    providers: list[Provider] = [config.provider]
    if config.fallback_enabled:
        providers.append("groq" if config.provider == "gemini" else "gemini")
    for index, provider in enumerate(providers):
        try:
            model = create_model(provider, config)
            result = await assistant.chat(graph, model, messages, changes=changes, context=context)
        except assistant.AssistantUnavailableError as exc:
            if not exc.retryable or index == len(providers) - 1:
                logger.warning("ea_assistant_failed", provider=provider, retryable=exc.retryable)
                raise
            logger.warning("ea_assistant_fallback", provider=provider, next_provider=providers[1])
            continue
        selected = config.groq_model if provider == "groq" else config.gemini_model
        result.update(
            provider=provider,
            model=getattr(model, "model_name", selected),
            fallback_used=index > 0,
        )
        return result
    raise AssertionError("unreachable")

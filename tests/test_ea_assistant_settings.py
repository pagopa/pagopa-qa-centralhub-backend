from __future__ import annotations

# ruff: noqa: S101
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.config import settings
from app.deps import get_db
from app.main import app
from app.schemas.ea_assistant import AssistantConfig
from app.services.ea import assistant as a
from app.services.ea import assistant_settings as svc
from app.services.ea.graph_index import GraphIndex
from tests.ea_graph_fixture import kb


def test_groq_default_uses_the_replacement_for_retired_llama() -> None:
    from app.config import Settings

    assert AssistantConfig().groq_model == "openai/gpt-oss-120b"
    assert Settings.model_fields["groq_model"].default == "openai/gpt-oss-120b"


async def test_settings_defaults_preserve_gemini_and_do_not_create_a_row() -> None:
    db = AsyncMock()
    db.get.return_value = None
    result = await svc.get_config(db)
    assert result.provider == "gemini"
    assert result.gemini_model == settings.gemini_model
    assert result.fallback_enabled is False
    db.commit.assert_not_called()


@pytest.mark.parametrize(
    "fields",
    [{"provider": "invalid"}, {"groq_model": ""}, {"gemini_model": " "}, {"api_key": "secret"}],
)
def test_settings_validate_provider_models_and_forbid_secrets(fields: dict) -> None:
    with pytest.raises(ValidationError):
        AssistantConfig(**fields)


async def test_settings_reject_missing_active_or_fallback_secret() -> None:
    with (
        patch.object(settings, "groq_api_key", ""),
        patch.object(settings, "gemini_api_key", "key"),
    ):
        with pytest.raises(ValueError, match="GROQ_API_KEY"):
            await svc.save_config(AsyncMock(), AssistantConfig(provider="groq"))
        with pytest.raises(ValueError, match="GROQ_API_KEY"):
            await svc.save_config(AsyncMock(), AssistantConfig(fallback_enabled=True))


async def test_settings_persist_without_secrets() -> None:
    db = AsyncMock()
    config = AssistantConfig(provider="groq", groq_model="chosen-model")
    with patch.object(settings, "groq_api_key", "do-not-store"):
        await svc.save_config(db, config)
    db.execute.assert_awaited_once()
    db.commit.assert_awaited_once()
    statement = db.execute.call_args.args[0]
    assert "do-not-store" not in str(statement.compile().params)
    assert statement.compile().params["provider"] == "groq"


class Model:
    def __init__(self, error: a.AssistantUnavailableError | None = None) -> None:
        self.error = error
        self.histories: list[list[a.Msg]] = []

    async def step(self, system, history, tools):
        self.histories.append(list(history))
        if self.error:
            raise self.error
        return a.ModelTurn(text="Ecco [[prod:gpd]].")


async def test_optional_fallback_restarts_and_reports_effective_provider() -> None:
    first = Model(a.AssistantUnavailableError("busy", retryable=True))
    second = Model()
    config = AssistantConfig(provider="groq", fallback_enabled=True)
    with patch.object(svc, "create_model", side_effect=[first, second]):
        result = await svc.answer(GraphIndex(kb()), config, [{"role": "user", "content": "GPD?"}])
    assert result["provider"] == "gemini"
    assert result["model"] == config.gemini_model
    assert result["fallback_used"] is True
    assert len(second.histories[0]) == 1


@pytest.mark.parametrize("enabled,retryable", [(False, True), (True, False)])
async def test_fallback_is_not_used_when_disabled_or_error_is_not_transient(
    enabled: bool, retryable: bool
) -> None:
    model = Model(a.AssistantUnavailableError("failed", retryable=retryable))
    with patch.object(svc, "create_model", return_value=model) as factory:
        with pytest.raises(a.AssistantUnavailableError):
            await svc.answer(
                GraphIndex(kb()),
                AssistantConfig(fallback_enabled=enabled),
                [{"role": "user", "content": "GPD?"}],
            )
    assert factory.call_count == 1


async def test_fallback_does_not_loop_when_both_providers_fail() -> None:
    with patch.object(
        svc, "create_model", return_value=Model(a.AssistantUnavailableError("busy", retryable=True))
    ) as factory:
        with pytest.raises(a.AssistantUnavailableError, match="busy"):
            await svc.answer(
                GraphIndex(kb()),
                AssistantConfig(fallback_enabled=True),
                [{"role": "user", "content": "GPD?"}],
            )
    assert factory.call_count == 2


async def test_fallback_discards_partial_tool_history() -> None:
    class PartialModel:
        count = 0

        async def step(self, system, history, tools):
            self.count += 1
            if self.count == 1:
                return a.ModelTurn(calls=[a.ToolCall("cerca_nodi", {"testo": "GPD"}, "one")])
            raise a.AssistantUnavailableError("busy", retryable=True)

    second = Model()
    with patch.object(svc, "create_model", side_effect=[PartialModel(), second]):
        result = await svc.answer(
            GraphIndex(kb()),
            AssistantConfig(fallback_enabled=True),
            [{"role": "user", "content": "GPD?"}],
        )
    assert len(second.histories[0]) == 1
    assert result["steps"] == []
    assert result["provider"] == "groq"


async def test_settings_read_saved_values_instead_of_environment_defaults() -> None:
    from app.models.ea_assistant import EaAssistantSettings

    db = AsyncMock()
    db.get.return_value = EaAssistantSettings(
        id=1,
        provider="groq",
        gemini_model="other-gemini",
        groq_model="chosen",
        fallback_enabled=True,
    )
    config = await svc.get_config(db)
    assert config.provider == "groq"
    assert config.groq_model == "chosen"
    assert config.fallback_enabled is True


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_settings_api_never_returns_secrets(client, method: str) -> None:
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch.object(svc, "get_config", new_callable=AsyncMock, return_value=AssistantConfig()),
            patch.object(
                svc, "save_config", new_callable=AsyncMock, return_value=AssistantConfig()
            ),
        ):
            response = await client.request(
                method,
                "/api/v1/ea/assistant/settings",
                **({"json": AssistantConfig().model_dump()} if method == "PUT" else {}),
            )
        assert response.status_code == 200
        assert set(response.json()["configured"]) == {"gemini", "groq"}
        assert "api_key" not in response.text.lower()
    finally:
        app.dependency_overrides.clear()


async def test_settings_api_rejects_a_secret_in_the_payload(client) -> None:
    response = await client.put(
        "/api/v1/ea/assistant/settings",
        json={**AssistantConfig().model_dump(), "api_key": "must-not-be-stored"},
    )
    assert response.status_code == 422


async def test_settings_api_rejects_unconfigured_provider_without_writing(client) -> None:
    db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.object(settings, "groq_api_key", ""):
            response = await client.put(
                "/api/v1/ea/assistant/settings",
                json=AssistantConfig(provider="groq").model_dump(),
            )
        assert response.status_code == 422
        assert "GROQ_API_KEY" in response.json()["detail"]
        db.execute.assert_not_called()
        db.commit.assert_not_called()
    finally:
        app.dependency_overrides.clear()


async def test_chat_reloads_provider_on_each_request(client) -> None:
    with (
        patch.object(settings, "gemini_api_key", "test"),
        patch.object(settings, "groq_api_key", "test"),
        patch("app.api.v1.ea._github", return_value=None),
        patch("app.services.ea.kb.build_kb", new_callable=AsyncMock, return_value=kb()),
        patch.object(svc, "create_model", return_value=Model()) as factory,
        patch.object(
            svc,
            "get_config",
            new_callable=AsyncMock,
            side_effect=[
                AssistantConfig(provider="gemini"),
                AssistantConfig(provider="groq"),
            ],
        ),
    ):
        first = await client.post(
            "/api/v1/ea/assistant/chat", json={"messages": [{"role": "user", "content": "GPD?"}]}
        )
        second = await client.post(
            "/api/v1/ea/assistant/chat", json={"messages": [{"role": "user", "content": "GPD?"}]}
        )
    assert first.status_code == second.status_code == 200
    assert first.json()["provider"] == "gemini"
    assert second.json()["provider"] == "groq"
    assert [call.args[0] for call in factory.call_args_list] == ["gemini", "groq"]

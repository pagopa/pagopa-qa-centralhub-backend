from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def test_build_prompt_italian():
    from app.services.bdd_ai import build_prompt
    prompt = build_prompt("Utente accede con credenziali valide", "Login test", "it", 3)
    assert "Gherkin" in prompt
    assert "3" in prompt
    assert "italiano" in prompt.lower()


def test_build_prompt_english():
    from app.services.bdd_ai import build_prompt
    prompt = build_prompt("User logs in with valid credentials", "Login test", "en", 5)
    assert "English" in prompt


def test_get_system_prompt_it():
    from app.services.bdd_ai import get_system_prompt
    prompt = get_system_prompt("it")
    assert "Funzionalità" in prompt


def test_get_system_prompt_en():
    from app.services.bdd_ai import get_system_prompt
    prompt = get_system_prompt("en")
    assert "Feature" in prompt


def test_get_ai_provider_returns_ollama():
    from app.services.bdd_ai import get_ai_provider, OllamaProvider
    p = get_ai_provider("ollama", None, "claude-sonnet-4-6", "http://localhost:11434", "llama3.2")
    assert isinstance(p, OllamaProvider)


def test_get_ai_provider_claude_missing_key_raises():
    from app.services.bdd_ai import get_ai_provider
    with pytest.raises(ValueError, match="Claude API key"):
        get_ai_provider("claude", None, "claude-sonnet-4-6", "", "")


def test_get_ai_provider_returns_claude():
    from app.services.bdd_ai import get_ai_provider, ClaudeProvider
    p = get_ai_provider("claude", "sk-ant-test", "claude-sonnet-4-6", "", "")
    assert isinstance(p, ClaudeProvider)

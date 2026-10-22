from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Protocol

import httpx
import structlog

logger = structlog.get_logger(__name__)


class AIProvider(Protocol):
    async def generate_stream(self, system_prompt: str, user_prompt: str) -> AsyncGenerator[str, None]:
        ...


SYSTEM_PROMPT_IT = """Sei un esperto QA Engineer specializzato in BDD (Behavior-Driven Development).
Il tuo compito è generare scenari Gherkin a partire da requisiti software.

Regole:
- Usa keyword in italiano: Funzionalità, Scenario, Schema dello scenario, Dato, Quando, Allora, E, Ma, Esempi
- Ogni scenario deve avere tag tra @happy-path, @negative, @edge-case, @smoke, @regression
- Usa Scenario Outline con tabella Esempi quando ha senso parametrizzare
- Rispondi SOLO con JSON valido nel formato: {"scenarios": [{"title": "...", "gherkin": "..."}]}
- Il campo gherkin deve contenere il testo Gherkin completo incluso Feature: e Scenario:
- Non aggiungere testo fuori dal JSON"""

SYSTEM_PROMPT_EN = """You are an expert QA Engineer specialized in BDD (Behavior-Driven Development).
Your task is to generate Gherkin scenarios from software requirements.

Rules:
- Use English Gherkin keywords: Feature, Scenario, Scenario Outline, Given, When, Then, And, But, Examples
- Each scenario must have tags from: @happy-path, @negative, @edge-case, @smoke, @regression
- Use Scenario Outline with Examples table when parameterization makes sense
- Reply ONLY with valid JSON in this format: {"scenarios": [{"title": "...", "gherkin": "..."}]}
- The gherkin field must contain the full Gherkin text including Feature: and Scenario:
- Do not add any text outside the JSON"""


def build_prompt(requirement: str, title: str, language: str, max_scenarios: int) -> str:
    try:
        lang_note = "italiano" if language == "it" else "English"
        return (
            f"Titolo del requisito: {title}\n\n"
            f"Requisito:\n{requirement}\n\n"
            f"Genera al massimo {max_scenarios} scenari Gherkin in {lang_note}."
        )
    except Exception:
        logger.exception("build_prompt failed")
        raise


def get_system_prompt(language: str) -> str:
    try:
        return SYSTEM_PROMPT_IT if language == "it" else SYSTEM_PROMPT_EN
    except Exception:
        logger.exception("get_system_prompt failed")
        raise


class ClaudeProvider:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6") -> None:
        try:
            self._api_key = api_key
            self._model = model
        except Exception:
            logger.exception("ClaudeProvider.__init__ failed")
            raise

    async def generate_stream(self, system_prompt: str, user_prompt: str) -> AsyncGenerator[str, None]:
        try:
            import anthropic

            has_results = False
            client = anthropic.AsyncAnthropic(api_key=self._api_key)
            async with client.messages.stream(
                model=self._model,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    has_results = has_results or bool(text)
                    yield text
            if not has_results:
                logger.info("ClaudeProvider.generate_stream query succeeded with 0 results")
        except Exception:
            logger.exception("ClaudeProvider.generate_stream failed")
            raise


class OllamaProvider:
    def __init__(self, base_url: str, model: str) -> None:
        try:
            self._base_url = base_url.rstrip("/")
            self._model = model
        except Exception:
            logger.exception("OllamaProvider.__init__ failed")
            raise

    async def generate_stream(self, system_prompt: str, user_prompt: str) -> AsyncGenerator[str, None]:
        try:
            has_results = False
            payload = {
                "model": self._model,
                "prompt": user_prompt,
                "system": system_prompt,
                "stream": True,
            }
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream("POST", f"{self._base_url}/api/generate", json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            chunk = data.get("response", "")
                            if chunk:
                                has_results = True
                                yield chunk
                            if data.get("done"):
                                break
                        except json.JSONDecodeError:
                            logger.exception("OllamaProvider.generate_stream failed")
                            continue
            if not has_results:
                logger.info("OllamaProvider.generate_stream query succeeded with 0 results")
        except Exception:
            logger.exception("OllamaProvider.generate_stream failed")
            raise


def get_ai_provider(ai_provider: str, claude_api_key: str | None, claude_model: str,
                    ollama_base_url: str, ollama_model: str) -> ClaudeProvider | OllamaProvider:
    try:
        if ai_provider == "claude":
            if not claude_api_key:
                raise ValueError("Claude API key not configured")
            return ClaudeProvider(api_key=claude_api_key, model=claude_model)
        return OllamaProvider(base_url=ollama_base_url, model=ollama_model)
    except Exception:
        logger.exception("get_ai_provider failed")
        raise

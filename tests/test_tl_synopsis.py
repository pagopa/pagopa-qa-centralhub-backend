# ruff: noqa: F811, S101
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.ea.assistant import AssistantUnavailableError
from app.services.test_lifecycle import synopsis as svc
from tests.test_tl_features import BASE, _use_test_db, actor, scope  # noqa: F401
from tests.test_tl_workflow import _ready_feature, reviewer  # noqa: F401

FEATURE = """﻿# qa-hub: FEAT_001_Pay
# language: it
@tag1
Funzionalità: Pagamento

  # commento
  @s1
  Scenario: ok
    Dato x


  Scenario Outline: ko
    Dato <a>
    Esempi:
      | a |
      | 1 |
      | 2 |
  \"\"\"
  # dentro docstring
  Scenario: finto
  \"\"\"
"""


def test_clean_drops_tags_comments_and_keeps_docstrings():
    cleaned = svc.clean_feature_text(FEATURE)
    assert "@tag1" not in cleaned and "# commento" not in cleaned and "qa-hub" not in cleaned
    assert "# dentro docstring" in cleaned
    assert "\n\n\n" not in cleaned and not cleaned.startswith("\n")


def test_count_scenarios_ignores_examples_rows_and_docstrings():
    assert svc.count_scenarios(svc.clean_feature_text(FEATURE)) == 2


@pytest.mark.parametrize(
    ("n", "words"),
    [(0, 30), (1, 30), (2, 34), (4, 43), (8, 61), (11, 75), (12, 80), (50, 80)],
)
def test_max_words_matches_documented_table(n, words):
    assert svc.max_words_for(n) == words


def test_max_words_rejects_negative():
    with pytest.raises(ValueError):
        svc.max_words_for(-1)


def test_validate_normalizes_label_quotes_and_lines():
    assert svc.validate_synopsis('Sinossi: "Verifica il\n pagamento."', 10) == (
        "Verifica il pagamento."
    )


@pytest.mark.parametrize("raw", ["", "- elenco", "1. punto", "# titolo", "scrivi a @me", "a " * 11])
def test_validate_rejects(raw):
    with pytest.raises(svc.SynopsisRejectedError):
        svc.validate_synopsis(raw, 10)


async def test_endpoint_generates_from_preview(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    url = f"{BASE}/features/{detail['id']}/revisions/{detail['working_revision']['id']}/synopsis"
    fake = AsyncMock(return_value="Verifica il pagamento nei casi previsti.")
    with (
        patch.object(svc.settings, "gemini_api_key", "k"),
        patch("app.services.ea.assistant.GeminiModel.generate_text", fake),
    ):
        resp = await client.post(url)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["synopsis"] == "Verifica il pagamento nei casi previsti."
    assert out["max_words"] == 30
    prompt = fake.await_args.args[0]
    assert "Allora il pagamento è confermato" in prompt and "qa-hub:" not in prompt


async def test_endpoint_retries_once_then_rejects(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    url = f"{BASE}/features/{detail['id']}/revisions/{detail['working_revision']['id']}/synopsis"
    fake = AsyncMock(side_effect=["- elenco", "ancora\n- elenco"])
    with (
        patch.object(svc.settings, "gemini_api_key", "k"),
        patch("app.services.ea.assistant.GeminiModel.generate_text", fake),
    ):
        resp = await client.post(url)
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "synopsis_invalid"
    assert fake.await_count == 2
    assert "Il tentativo precedente" in fake.await_args_list[1].args[0]


async def test_endpoint_without_key_returns_placeholder_without_calling_gemini(
    client, scope, actor, reviewer
):
    detail = await _ready_feature(client, scope, actor, reviewer)
    url = f"{BASE}/features/{detail['id']}/revisions/{detail['working_revision']['id']}/synopsis"
    fake = AsyncMock()
    with (
        patch.object(svc.settings, "gemini_api_key", ""),
        patch("app.services.ea.assistant.GeminiModel.generate_text", fake),
    ):
        resp = await client.post(url)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["synopsis"].startswith("Lorem ipsum dolor sit amet")
    assert out["model"] == "placeholder"
    assert len(out["synopsis"].split()) <= out["max_words"]
    fake.assert_not_awaited()

async def test_endpoint_provider_failure_is_502(client, scope, actor, reviewer):
    detail = await _ready_feature(client, scope, actor, reviewer)
    url = f"{BASE}/features/{detail['id']}/revisions/{detail['working_revision']['id']}/synopsis"
    fake = AsyncMock(side_effect=AssistantUnavailableError("giù", retryable=True))
    with (
        patch.object(svc.settings, "gemini_api_key", "k"),
        patch("app.services.ea.assistant.GeminiModel.generate_text", fake),
    ):
        resp = await client.post(url)
    assert resp.status_code == 502

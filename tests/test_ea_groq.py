from __future__ import annotations

# ruff: noqa: S101
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.ea import assistant as a
from app.services.ea import groq
from app.services.ea.graph_index import GraphIndex
from tests.ea_graph_fixture import kb


def response(*, calls: list[dict] | None = None, text: str = "") -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text, "tool_calls": calls}}]}


async def test_groq_runs_multiple_tools_and_preserves_call_ids() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=response(
                    calls=[
                        {
                            "id": "one",
                            "type": "function",
                            "function": {"name": "cerca_nodi", "arguments": '{"testo":"GPD"}'},
                        },
                        {
                            "id": "two",
                            "type": "function",
                            "function": {"name": "evidenzia", "arguments": '{"ids":["prod:gpd"]}'},
                        },
                    ]
                ),
            )
        return httpx.Response(200, json=response(text="Ecco [[prod:gpd]]."))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = groq.GroqModel("test-key", "test-model", client=client)
        result = await a.chat(GraphIndex(kb()), model, [{"role": "user", "content": "GPD?"}])
    assert result["answer"] == "Ecco [[prod:gpd]]."
    tools = requests[0]["tools"]
    assert tools[0]["function"]["parameters"]["type"] == "object"
    assert tools[0]["function"]["parameters"]["properties"]["testo"]["type"] == "string"
    assert [m["tool_call_id"] for m in requests[1]["messages"] if m["role"] == "tool"] == [
        "one",
        "two",
    ]
    assert requests[1]["messages"][2]["tool_calls"][0]["id"] == "one"
    assert a.TOOLS[0]["parameters"]["type"] == "OBJECT"
    for request in requests:
        prompt = request["messages"][0]["content"]
        assert a.SYSTEM_PROMPT in prompt
        assert "ERRATO:" in prompt
        assert "CORRETTO:" in prompt
        assert "Ogni elemento della mappa citato" in prompt
        assert "Non inventare" in prompt
        assert "[[pagopa-wisp-converter]]" in prompt
        assert "Non rispondere con una lista di soli riferimenti" in prompt
        assert "Sostituisci solo il nome" in prompt
        assert "la relazione o il motivo" in prompt
        assert "CORRETTO: [[pagopa-wisp-converter]]." not in prompt


@pytest.mark.parametrize("code,retryable", [(401, False), (400, False), (429, True), (503, True)])
async def test_groq_classifies_errors_and_bounds_retries(code: int, retryable: bool) -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(code, json={"error": {"message": "do not expose test-key"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = groq.GroqModel("test-key", "model", client=client)
        with patch("app.services.ea.groq.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(a.AssistantUnavailableError) as caught:
                await model.step("system", [a.Msg("user", "ciao")], a.TOOLS)
    assert caught.value.retryable is retryable
    assert "test-key" not in str(caught.value)
    assert count == (2 if code == 503 else 1)


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        response(text=""),
        response(calls=[{"id": "x", "function": {"name": "cerca_nodi", "arguments": "not json"}}]),
        response(calls=[{"id": "x", "function": {"name": "cerca_nodi", "arguments": "[]"}}]),
    ],
)
async def test_groq_rejects_invalid_responses(payload: dict) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        model = groq.GroqModel("key", "model", client=client)
        with pytest.raises(a.AssistantUnavailableError, match="risposta"):
            await model.step("system", [a.Msg("user", "ciao")], a.TOOLS)


def test_groq_requires_a_secret() -> None:
    with pytest.raises(a.AssistantUnavailableError, match="GROQ_API_KEY"):
        groq.GroqModel("", "model")


async def test_groq_transport_failure_is_eligible_for_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("test", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with patch("app.services.ea.groq.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with pytest.raises(a.AssistantUnavailableError) as caught:
                await groq.GroqModel("key", "model", client=client).step(
                    "system", [a.Msg("user", "ciao")], a.TOOLS
                )
    assert caught.value.retryable
    sleep.assert_awaited_once()


async def test_groq_rate_limit_reports_retry_after_without_retrying() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                429,
                json={"error": {"message": "secret provider payload"}},
                headers={"retry-after": "12", "x-ratelimit-remaining-tokens": "0"},
            )
        )
    ) as client:
        with patch("app.services.ea.groq.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with pytest.raises(a.AssistantUnavailableError) as caught:
                await groq.GroqModel("key", "model", client=client).step(
                    "system", [a.Msg("user", "ciao")], a.TOOLS
                )
    assert "12" in str(caught.value)
    assert "429" in str(caught.value)
    assert "secret provider payload" not in str(caught.value)
    assert caught.value.retryable
    sleep.assert_not_awaited()

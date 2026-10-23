from __future__ import annotations

# ruff: noqa: S101
from typing import Any

from app.services.ea import assistant as a
from app.services.ea.graph_index import GraphIndex
from tests.ea_graph_fixture import kb


class ScriptedModel:
    """Plays back a fixed list of turns and records what it was shown."""

    def __init__(self, turns: list[a.ModelTurn]) -> None:
        self.turns = turns
        self.seen: list[list[a.Msg]] = []
        self.systems: list[str] = []

    async def step(
        self, system: str, history: list[a.Msg], tools: list[dict[str, Any]]
    ) -> a.ModelTurn:
        self.seen.append(list(history))
        self.systems.append(system)
        return self.turns.pop(0)


async def test_chat_runs_tools_highlights_and_keeps_known_refs() -> None:
    model = ScriptedModel(
        [
            a.ModelTurn(calls=[a.ToolCall("cerca_nodi", {"testo": "GPD Core"})]),
            a.ModelTurn(
                calls=[
                    a.ToolCall("impatto", {"id": "pagopa-debt-position", "direzione": "chiamanti"})
                ]
            ),
            a.ModelTurn(
                calls=[
                    a.ToolCall(
                        "evidenzia",
                        {"ids": ["pagopa-debt-position", "pagopa-wisp-converter", "boh"]},
                    )
                ]
            ),
            a.ModelTurn(
                text="Lo chiamano [[pagopa-wisp-converter]] e [[pagopa-gpd-payments]], "
                "non [[inventato]]."
            ),
        ]
    )
    res = await a.chat(
        GraphIndex(kb()),
        model,
        [{"role": "user", "content": "Chi si rompe se cambio GPD?"}],
        context={"view": "map", "node_id": "prod:gpd"},
    )
    assert (
        res["answer"]
        == "Lo chiamano [[pagopa-wisp-converter]] e [[pagopa-gpd-payments]], non inventato."
    )
    assert res["highlight"] == [
        "pagopa-debt-position",
        "pagopa-wisp-converter",
        "pagopa-gpd-payments",
    ]
    assert res["labels"]["pagopa-gpd-payments"] == "GPD Payments"
    assert [s["tool"] for s in res["steps"]] == ["cerca_nodi", "impatto", "evidenzia"]
    assert "[[prod:gpd]]" in model.systems[0]
    # the tool result reached the model on the next turn
    impact_result = model.seen[2][-1]
    assert impact_result.role == "tool"
    assert {x["id"] for x in impact_result.tool_result["chiamanti"]} == {
        "pagopa-wisp-converter",
        "pagopa-gpd-payments",
        "pagopa-checkout-fe",
    }
    assert impact_result.tool_result["dati_a_valle"][0]["id"] == "dl:gpd-ingestion"


async def test_tools_report_unknown_ids_and_bad_arguments() -> None:
    t = a.Tools(GraphIndex(kb()), None)
    r = await t.run(a.ToolCall("scheda_nodo", {"id": "gpd"}))
    assert "errore" in r and r["forse_cercavi"][0]["id"] == "prod:gpd"
    assert "errore" in await t.run(a.ToolCall("scheda_nodo", {"nope": 1}))
    assert "errore" in await t.run(a.ToolCall("sconosciuto", {}))
    assert (await t.run(a.ToolCall("impatto_rilascio", {"url": "x"})))["errore"].startswith(
        "GitHub"
    )


async def test_product_impact_and_qa_gaps() -> None:
    t = a.Tools(GraphIndex(kb()), None)
    r = await t.run(
        a.ToolCall("impatto", {"id": "prod:gpd", "direzione": "entrambe", "profondita": 9})
    )
    assert r["profondita"] == 4
    assert {x["id"] for x in r["chiamanti"]} == {"pagopa-wisp-converter", "pagopa-checkout-fe"}
    gaps = await t.run(a.ToolCall("lacune_qa", {"ambito": "area:pay", "maturita_massima": 1}))
    assert [x["id"] for x in gaps["componenti"]] == ["pagopa-checkout-fe"]
    gaps = await t.run(a.ToolCall("lacune_qa", {"senza_integrazione": True, "maturita_massima": 5}))
    assert {x["id"] for x in gaps["componenti"]} == {"pagopa-checkout-fe", "pagopa-gpd-payments"}


async def test_chat_stops_after_max_steps() -> None:
    model = ScriptedModel(
        [a.ModelTurn(calls=[a.ToolCall("cerca_nodi", {"testo": "x"})]) for _ in range(a.MAX_STEPS)]
    )
    res = await a.chat(GraphIndex(kb()), model, [{"role": "user", "content": "?"}])
    assert res["answer"].startswith("Non sono riuscito")


def test_gemini_model_requires_a_key() -> None:
    try:
        a.GeminiModel("", "gemini-3.8-flash")
    except a.AssistantUnavailableError as exc:
        assert "GEMINI_API_KEY" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected AssistantUnavailableError")


def test_gemini_contents_group_tool_results() -> None:
    hist = [
        a.Msg(role="user", text="ciao"),
        a.Msg(
            role="model",
            calls=[
                a.ToolCall("cerca_nodi", {"testo": "a"}),
                a.ToolCall("cerca_nodi", {"testo": "b"}),
            ],
        ),
        a.Msg(role="tool", tool_name="cerca_nodi", tool_result={"risultati": []}),
        a.Msg(role="tool", tool_name="cerca_nodi", tool_result={"risultati": []}),
    ]
    hist.append(a.Msg(role="user", text="e poi?"))
    contents = a.GeminiModel._contents(hist)
    assert [c.role for c in contents] == ["user", "model", "user", "user"]
    assert len(contents[2].parts) == 2
    assert all(p.function_response for p in contents[2].parts)
    assert contents[3].parts[0].text == "e poi?"


class _OverloadedError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"{code} UNAVAILABLE")
        self.code = code


class _FakeModels:
    """Stands in for ``client.aio.models``: fails with the given codes, then answers."""

    def __init__(self, failures: dict[str, list[int]]) -> None:
        self.failures = failures
        self.calls: list[str] = []

    async def generate_content(self, model: str, contents: Any, config: Any) -> str:
        self.calls.append(model)
        queue = self.failures.get(model, [])
        if queue:
            raise _OverloadedError(queue.pop(0))
        return f"ok:{model}"


def _gemini(
    failures: dict[str, list[int]], fallbacks: list[str] | None = None
) -> tuple[a.GeminiModel, _FakeModels, list[float]]:
    m = a.GeminiModel("key", "main", fallbacks)
    fake = _FakeModels(failures)
    m._client = type("C", (), {"aio": type("A", (), {"models": fake})()})()  # type: ignore[assignment]
    slept: list[float] = []

    async def sleep(d: float) -> None:
        slept.append(d)

    m._sleep = sleep  # type: ignore[method-assign]
    return m, fake, slept


async def test_gemini_retries_when_overloaded() -> None:
    m, fake, slept = _gemini({"main": [503, 503]})
    assert await m._generate([], None) == "ok:main"
    assert fake.calls == ["main", "main", "main"]
    assert slept == [1.0, 3.0]


async def test_gemini_falls_back_and_then_sticks_to_the_model_that_answered() -> None:
    m, fake, _ = _gemini({"main": [503, 503, 503]}, ["main", "backup"])
    assert await m._generate([], None) == "ok:backup"
    assert fake.calls == ["main"] * 3 + ["backup"]
    assert await m._generate([], None) == "ok:backup"
    assert fake.calls[-1] == "backup" and len(fake.calls) == 5


async def test_gemini_quota_errors_move_to_the_next_model_at_once() -> None:
    m, fake, slept = _gemini({"main": [429]}, ["backup"])
    assert await m._generate([], None) == "ok:backup"
    assert fake.calls == ["main", "backup"] and slept == []


async def test_gemini_gives_a_clear_message_when_still_overloaded() -> None:
    m, _, _ = _gemini({"main": [503] * 3})
    try:
        await m._generate([], None)
    except a.AssistantUnavailableError as exc:
        assert "sovraccarico" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected AssistantUnavailableError")


def test_gemini_replays_its_own_message_with_thought_signatures() -> None:
    from google.genai import types

    raw = types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(name="cerca_nodi", args={"testo": "gpd"}),
                thought_signature=b"sig",
            )
        ],
    )
    hist = [
        a.Msg(role="user", text="ciao"),
        a.Msg(role="model", calls=[a.ToolCall("cerca_nodi", {"testo": "gpd"})], raw=raw),
        a.Msg(role="tool", tool_name="cerca_nodi", tool_result={"risultati": []}),
    ]
    contents = a.GeminiModel._contents(hist)
    assert contents[1] is raw
    assert contents[1].parts[0].thought_signature == b"sig"


async def test_gemini_does_not_retry_client_errors() -> None:
    m, fake, slept = _gemini({"main": [404]})
    try:
        await m._generate([], None)
    except a.AssistantUnavailableError as exc:
        assert "404" in str(exc)
    assert fake.calls == ["main"] and slept == []


async def test_gemini_transport_failure_is_eligible_for_provider_fallback() -> None:
    from unittest.mock import AsyncMock

    import httpx

    model, _, _ = _gemini({})
    model._client.aio.models.generate_content = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    try:
        await model._generate([], None)
    except a.AssistantUnavailableError as exc:
        assert exc.retryable
    else:
        raise AssertionError("expected AssistantUnavailableError")

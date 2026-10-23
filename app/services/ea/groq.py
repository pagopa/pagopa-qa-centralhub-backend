from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field, ValidationError

from app.services.ea.assistant import AssistantUnavailableError, ModelTurn, Msg, ToolCall

logger = structlog.get_logger(__name__)
_URL = "https://api.groq.com/openai/v1/chat/completions"
_TRANSIENT = frozenset({429, 500, 502, 503, 504})

_REFERENCE_INSTRUCTIONS = """

Formato obbligatorio della risposta finale:
- Rispondi alla domanda con testo descrittivo e riferimenti cliccabili insieme.
  Non rispondere con una lista di soli riferimenti o pulsanti.
- Sostituisci solo il nome dell'elemento con [[id_esatto]], non l'intera frase.
  Conserva la spiegazione: per ogni elemento o gruppo spiega la relazione o il motivo
  della sua rilevanza, usando solo i risultati degli strumenti. Se il motivo non e'
  disponibile, dichiaralo senza inventarlo.
- Ogni elemento della mappa citato deve essere scritto come [[id_esatto]], usando
  esclusivamente gli ID restituiti dagli strumenti. Non inventare ID, neppure dagli esempi.
- Il frontend trasforma [[id_esatto]] nel nome leggibile e in un pulsante cliccabile.
  Non sostituirlo con il nome, un ID tra backtick, grassetto o un link Markdown.
- Questi esempi mostrano solo la sintassi, non dimostrano relazioni nel grafo:
  ERRATO: WISP Converter dipende da GPD.
  CORRETTO: [[pagopa-wisp-converter]] dipende da [[prod:gpd]].
  ERRATO: [[pagopa-wisp-converter]], [[prod:gpd]].
  CORRETTO: [[pagopa-wisp-converter]] dipende da [[prod:gpd]]; una modifica
  dell'interfaccia richiede di verificare la compatibilita del chiamante.
- Per una domanda di impatto, distingui dipendenze dirette, indirette e dati a valle
  quando gli strumenti lo consentono. Un legame indica un impatto potenziale, non
  dimostra che il componente si rompera' certamente.
- Usa questa sintassi anche negli elenchi e per i dataset. Se non conosci l'ID,
  cercalo con cerca_nodi; se non viene trovato, dichiara che non e' stato identificato.
- Prima di concludere, verifica che i nomi degli elementi identificati siano
  sostituiti dai rispettivi [[id_esatto]]. La sola chiamata a evidenzia non sostituisce
  i riferimenti nel testo. Poi restituisci solo la risposta, senza questa checklist.
"""


class _Function(BaseModel):
    name: str = Field(min_length=1)
    arguments: str


class _Call(BaseModel):
    id: str = Field(min_length=1)
    function: _Function


class _Message(BaseModel):
    content: str | None = None
    tool_calls: list[_Call] | None = None


class _Choice(BaseModel):
    message: _Message
    finish_reason: str | None = None


class _Response(BaseModel):
    choices: list[_Choice] = Field(min_length=1, max_length=1)


def _schema(value: Any) -> Any:
    """Normalize Gemini's uppercase schema types without changing the shared tools."""
    if isinstance(value, dict):
        return {
            key: item.lower() if key == "type" and isinstance(item, str) else _schema(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_schema(item) for item in value]
    return value


class GroqModel:
    def __init__(
        self, api_key: str, model: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        if not api_key:
            raise AssistantUnavailableError(
                "Assistente non configurato: manca GROQ_API_KEY sul backend"
            )
        self._api_key = api_key
        self.model_name = model
        self._client = client

    @staticmethod
    def _messages(system: str, history: list[Msg]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for msg in history:
            if msg.role == "tool":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id,
                        "content": json.dumps(msg.tool_result, ensure_ascii=False),
                    }
                )
            elif msg.role == "model":
                item: dict[str, Any] = {"role": "assistant", "content": msg.text or None}
                if msg.calls:
                    item["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.args),
                            },
                        }
                        for call in msg.calls
                    ]
                messages.append(item)
            else:
                messages.append({"role": "user", "content": msg.text})
        return messages

    async def _generate(self, client: httpx.AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(2):
            try:
                resp = await client.post(
                    _URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=httpx.Timeout(30, connect=5),
                )
            except httpx.TransportError as exc:
                logger.warning("ea_assistant_transport_error", provider="groq", attempt=attempt + 1)
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                raise AssistantUnavailableError(
                    "Groq non raggiungibile: riprova tra qualche minuto", retryable=True
                ) from exc
            if resp.is_error:
                code = resp.status_code
                logger.warning("ea_assistant_provider_error", provider="groq", code=code)
                if code == 429:
                    retry_after = resp.headers.get("retry-after", "")
                    valid_delay = re.fullmatch(r"\d{1,8}(?:\.\d{1,6})?", retry_after)
                    wait = (
                        f": riprova tra {retry_after} secondi"
                        if valid_delay
                        else ": controlla i limiti del tuo account"
                    )
                    logger.warning(
                        "ea_assistant_rate_limit",
                        provider="groq",
                        model=self.model_name,
                        retry_after=retry_after if valid_delay else None,
                    )
                    raise AssistantUnavailableError(
                        f"Groq: limite di richieste o token raggiunto (HTTP 429){wait}",
                        retryable=True,
                    )
                if code in _TRANSIENT and code != 429 and attempt == 0:
                    await asyncio.sleep(1)
                    continue
                raise AssistantUnavailableError(
                    f"Groq non ha risposto (HTTP {code})", retryable=code in _TRANSIENT
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise AssistantUnavailableError(
                    "Groq ha restituito una risposta non valida"
                ) from exc
            if not isinstance(data, dict):
                raise AssistantUnavailableError("Groq ha restituito una risposta non valida")
            return data
        raise AssertionError("unreachable")

    async def step(self, system: str, history: list[Msg], tools: list[dict[str, Any]]) -> ModelTurn:
        payload = {
            "model": self.model_name,
            "messages": self._messages(system + _REFERENCE_INSTRUCTIONS, history),
            "tools": [{"type": "function", "function": _schema(tool)} for tool in tools],
            "tool_choice": "auto",
            "temperature": 0.2,
            "max_completion_tokens": 4096,
        }
        if self._client is not None:
            data = await self._generate(self._client, payload)
        else:
            async with httpx.AsyncClient() as client:
                data = await self._generate(client, payload)
        try:
            choice = _Response.model_validate(data).choices[0]
            message = choice.message
            calls = []
            for call in message.tool_calls or []:
                args = json.loads(call.function.arguments)
                if not isinstance(args, dict):
                    raise ValueError("tool arguments must be an object")
                calls.append(ToolCall(call.function.name, args, call.id))
            if choice.finish_reason == "length" or not (calls or (message.content or "").strip()):
                raise ValueError("empty or truncated response")
            if len({call.id for call in calls}) != len(calls):
                raise ValueError("duplicate tool call ids")
        except (ValidationError, ValueError) as exc:
            logger.warning("ea_assistant_invalid_response", provider="groq")
            raise AssistantUnavailableError("Groq ha restituito una risposta non valida") from exc
        return ModelTurn(text=message.content or "", calls=calls)

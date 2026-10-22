# ruff: noqa: E501  (system prompt and tool descriptions are prose for the model)
"""The EA Map assistant: questions in natural language, answered from the graph.

The model never sees the database: it can only call the read-only tools below, which
work on :class:`GraphIndex` (what the map shows) and on the release impact analysis.
The tools also let it choose which nodes to highlight on the map. Node references in
the answer are written as ``[[node-id]]`` and rendered by the frontend as clickable chips.

The model sits behind :class:`ChatModel`, so tests use a scripted fake; production uses
Gemini (``GEMINI_API_KEY``) with function calling.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from app.services.ea.graph_index import GraphIndex, Hop
from app.services.ea.release_impact import ChangeSource, ImpactError, parse_request, release_impact

logger = structlog.get_logger(__name__)

MAX_STEPS = 8
MAX_HISTORY = 12

SYSTEM_PROMPT = """Sei l’assistente della EA Map, la mappa di Enterprise Architecture della piattaforma \
pagamenti pagoPA dentro il QA Hub. Rispondi in italiano, in modo breve e concreto, a persone di QA, \
architettura e management.

Regole:
- Usa SOLO i dati restituiti dagli strumenti. Non inventare componenti, numeri, suite o legami. Se un \
dato non c’è, dillo.
- Prima di rispondere su un elemento, trovalo con cerca_nodi (gli id non si indovinano) e leggi la sua \
scheda o l’impatto.
- Quando citi un elemento della mappa scrivi il suo id tra doppie quadre, per esempio [[pagopa-debt-position]] \
o [[prod:gpd]]: l’interfaccia lo mostra come un collegamento cliccabile. Non ripetere il nome accanto.
- Prima della risposta finale chiama evidenzia con gli elementi più rilevanti (al massimo 25), così \
l’utente li vede accesi sulla mappa.
- «Chi si rompe se cambio X» = chi chiama X (strumento impatto, direzione chiamanti). «Da cosa dipende X» \
= direzione dipendenze. Per un link GitHub di PR o confronto usa impatto_rilascio.
- La maturità QA va da 0 a 5: un punto per test unitari, integration test, suite centrali o BDD del Nodo, \
performance, regressione notturna (NRT). Misura la presenza dei test, non il loro esito.
- Formato: frasi brevi ed elenchi puntati con «-». Grassetto con **testo** solo per i punti chiave. \
Niente tabelle, niente titoli."""


# ── neutral conversation format ──────────────────────────────────────────────


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class ModelTurn:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    #: the model's own message, replayed as is in the next request (Gemini needs the
    #: thought signatures attached to its function calls)
    raw: Any = None


@dataclass
class Msg:
    role: str  # "user" | "model" | "tool"
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    tool_name: str = ""
    tool_result: dict[str, Any] | None = None
    raw: Any = None


class ChatModel(Protocol):
    async def step(
        self, system: str, history: list[Msg], tools: list[dict[str, Any]]
    ) -> ModelTurn: ...


class AssistantUnavailableError(RuntimeError):
    """The assistant is not configured (no API key) or the model failed."""


# ── tools ────────────────────────────────────────────────────────────────────


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "OBJECT", "properties": props, "required": required or []}


S = {"type": "STRING"}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "cerca_nodi",
        "description": "Cerca nella mappa componenti, prodotti, aree, sistemi esterni, datastore, dataset, "
        "servizi e processi del Blueprint per nome, id o repository. Restituisce id, tipo e nome.",
        "parameters": _obj(
            {"testo": {**S, "description": "nome, sigla o repository, es. GPD, checkout, wisp"}},
            ["testo"],
        ),
    },
    {
        "name": "scheda_nodo",
        "description": "Dettaglio di un elemento: descrizione, prodotto, area, owner, stack, maturità QA e test, "
        "chi chiama e da chi è chiamato; per un prodotto i suoi componenti e processi SANP; per un processo i "
        "servizi e la pagina Confluence di riferimento.",
        "parameters": _obj(
            {"id": {**S, "description": "id esatto restituito da cerca_nodi"}}, ["id"]
        ),
    },
    {
        "name": "impatto",
        "description": "Analisi d’impatto sul grafo. direzione=chiamanti: chi chiama l’elemento, direttamente o "
        "indirettamente (chi si rompe se cambia). direzione=dipendenze: cosa chiama. Include i dataset del Data Lake "
        "alimentati. Per un prodotto analizza tutti i suoi componenti.",
        "parameters": _obj(
            {
                "id": S,
                "direzione": {**S, "enum": ["chiamanti", "dipendenze", "entrambe"]},
                "profondita": {"type": "INTEGER", "description": "da 1 a 4, default 2"},
            },
            ["id"],
        ),
    },
    {
        "name": "lacune_qa",
        "description": "Componenti runtime (microservizi, functions, frontend) con copertura di test bassa, "
        "eventualmente limitati a un prodotto o un’area. Ordinati dal più scoperto.",
        "parameters": _obj(
            {
                "ambito": {**S, "description": "id di prodotto o area, opzionale"},
                "maturita_massima": {"type": "INTEGER", "description": "default 1"},
                "senza_integrazione": {
                    "type": "BOOLEAN",
                    "description": "solo chi non ha test di integrazione",
                },
            }
        ),
    },
    {
        "name": "impatto_rilascio",
        "description": "Impatto di una pull request o di un confronto GitHub dell’organizzazione pagopa: file "
        "cambiati per categoria, rischio, componenti e dati impattati, suite da eseguire, lacune di test, processi "
        "SANP coinvolti.",
        "parameters": _obj({"url": {**S, "description": "URL della PR o del confronto"}}, ["url"]),
    },
    {
        "name": "evidenzia",
        "description": "Accende sulla mappa gli elementi indicati (id esatti). Chiamalo prima della risposta finale.",
        "parameters": _obj({"ids": {"type": "ARRAY", "items": S}}, ["ids"]),
    },
]


class Tools:
    def __init__(self, g: GraphIndex, changes: ChangeSource | None) -> None:
        self.g = g
        self.changes = changes
        self.highlight: list[str] = []
        self.steps: list[dict[str, str]] = []

    def _step(self, tool: str, label: str) -> None:
        self.steps.append({"tool": tool, "label": label})

    async def run(self, call: ToolCall) -> dict[str, Any]:
        fn: Callable[..., Awaitable[dict[str, Any]]] | None = getattr(self, f"t_{call.name}", None)
        if fn is None:
            return {"errore": f"strumento sconosciuto: {call.name}"}
        try:
            return await fn(**{k: v for k, v in (call.args or {}).items() if v is not None})
        except TypeError as exc:
            return {"errore": f"argomenti non validi: {exc}"}
        except ImpactError as exc:
            return {"errore": str(exc)}

    def _unknown(self, node_id: str) -> dict[str, Any]:
        hits = self.g.search(node_id, limit=5)
        return {"errore": f"id «{node_id}» non trovato", "forse_cercavi": hits}

    async def t_cerca_nodi(self, testo: str) -> dict[str, Any]:
        self._step("cerca_nodi", f"Cercato «{testo}»")
        return {"risultati": self.g.search(testo, limit=10)}

    async def t_scheda_nodo(self, id: str) -> dict[str, Any]:  # noqa: A002
        s = self.g.summary(id)
        if s is None:
            return self._unknown(id)
        self._step("scheda_nodo", f"Letta la scheda di {self.g.label(id)}")
        return s

    async def t_impatto(
        self,
        id: str,  # noqa: A002
        direzione: str = "chiamanti",
        profondita: int = 2,
    ) -> dict[str, Any]:
        if self.g.node(id) is None:
            return self._unknown(id)
        depth = max(1, min(int(profondita or 2), 4))
        n = self.g.nodes[id]
        starts = [c["id"] for c in self.g.components_of(id)] if n["type"] == "product" else [id]
        out: dict[str, Any] = {"elemento": {"id": id, "nome": n["name"]}, "profondita": depth}

        def collect(walk: Callable[[str], list[Hop]]) -> list[dict[str, Any]]:
            best: dict[str, Any] = {}
            for s in starts:
                for h in walk(s):
                    if h.id in starts:
                        continue
                    if h.id not in best or h.depth < best[h.id]["distanza"]:
                        node = self.g.nodes[h.id]
                        best[h.id] = {
                            "id": h.id,
                            "nome": node["name"],
                            "tipo": node["type"],
                            "distanza": h.depth,
                            "percorso": [self.g.label(x) for x in h.path],
                            "maturita_qa": (node.get("qa") or {}).get("score")
                            if node["type"] == "component"
                            else None,
                        }
            return sorted(best.values(), key=lambda x: (x["distanza"], x["nome"]))[:60]

        if direzione in ("chiamanti", "entrambe"):
            out["chiamanti"] = collect(lambda s: self.g.callers(s, depth))
            out["dati_a_valle"] = collect(lambda s: self.g.downstream_data(s, depth + 1))
        if direzione in ("dipendenze", "entrambe"):
            out["dipendenze"] = collect(lambda s: self.g.dependencies(s, depth))
        verb = {
            "chiamanti": "chi dipende da",
            "dipendenze": "da cosa dipende",
            "entrambe": "l’impatto di",
        }.get(direzione, "l’impatto di")
        self._step("impatto", f"Calcolato {verb} {n['name']}")
        return out

    async def t_lacune_qa(
        self, ambito: str | None = None, maturita_massima: int = 1, senza_integrazione: bool = False
    ) -> dict[str, Any]:
        comps = [
            n
            for n in self.g.nodes.values()
            if n["type"] == "component" and n.get("kind") in ("be", "fn", "fe")
        ]
        if ambito:
            if self.g.node(ambito) is None:
                return self._unknown(ambito)
            comps = [
                c
                for c in comps
                if c.get("parent") == ambito or (self.g.area_of(c["id"]) or {}).get("id") == ambito
            ]
        out = []
        for c in comps:
            qa = c.get("qa") or {}
            integ = bool(
                qa.get("it_repo") or qa.get("it_wf") or qa.get("central") or qa.get("nodo_bdd")
            )
            if qa.get("score", 0) > int(maturita_massima) or (senza_integrazione and integ):
                continue
            prod = self.g.product_of(c["id"])
            out.append(
                {
                    "id": c["id"],
                    "nome": c["name"],
                    "prodotto": prod["name"] if prod else None,
                    "maturita_qa": qa.get("score", 0),
                    "integration_test": integ,
                    "chiamato_da": len(
                        [
                            e
                            for e in self.g.inc[c["id"]]
                            if e["type"] in ("api", "soap", "event", "ui")
                        ]
                    ),
                }
            )
        out.sort(key=lambda x: (x["maturita_qa"], -x["chiamato_da"], x["nome"]))
        self._step(
            "lacune_qa",
            "Cercate le lacune di test" + (f" in {self.g.label(ambito)}" if ambito else ""),
        )
        return {"totale": len(out), "componenti": out[:40]}

    async def t_impatto_rilascio(self, url: str) -> dict[str, Any]:
        if self.changes is None:
            return {"errore": "GitHub non configurato sul QA Hub"}
        res = await release_impact(self.g, self.changes, parse_request(url=url))
        self._step(
            "impatto_rilascio", f"Analizzata {res['change']['ref']} di {res['change']['repo']}"
        )
        return {
            "modifica": res["change"],
            "componente": res["component"],
            "rischio": res["risk"],
            "categorie": [{"categoria": c["label"], "file": c["files"]} for c in res["categories"]],
            "impattati": [
                {k: x[k] for k in ("id", "name", "relation", "depth", "risk", "qa_score")}
                for x in res["impacted"]
            ][:40],
            "suite": [
                {"nome": s["name"], "tipo": s["kind"], "copre": s["covers"]} for s in res["suites"]
            ][:20],
            "lacune": res["gaps"],
            "processi": [
                {"id": p["id"], "nome": p["name"], "servizi": p["services"]}
                for p in res["processes"]
            ],
        }

    async def t_evidenzia(self, ids: list[str]) -> dict[str, Any]:
        ok = [i for i in ids if self.g.node(i) is not None or i in self.g.bp][:25]
        for i in ok:
            if i not in self.highlight:
                self.highlight.append(i)
        self._step("evidenzia", f"Evidenziati {len(ok)} elementi sulla mappa")
        return {"evidenziati": ok, "ignorati": [i for i in ids if i not in ok]}


# ── loop ─────────────────────────────────────────────────────────────────────

_REF_RX = re.compile(r"\[\[([^\[\]]+)\]\]")


def _context_line(g: GraphIndex, context: dict[str, Any] | None) -> str:
    if not context:
        return ""
    node_id = context.get("node_id")
    view = context.get("view")
    parts = []
    if view:
        parts.append(f"vista «{view}»")
    if node_id and (g.node(node_id) is not None or node_id in g.bp):
        parts.append(f"elemento selezionato [[{node_id}]] ({g.label(node_id)})")
    return f"\n\nContesto: l’utente sta guardando {', '.join(parts)}." if parts else ""


async def chat(
    g: GraphIndex,
    model: ChatModel,
    messages: list[dict[str, str]],
    changes: ChangeSource | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tools = Tools(g, changes)
    history = [
        Msg(role="user" if m["role"] == "user" else "model", text=m["content"])
        for m in messages[-MAX_HISTORY:]
    ]
    system = SYSTEM_PROMPT + _context_line(g, context)
    answer = ""
    for _ in range(MAX_STEPS):
        turn = await model.step(system, history, TOOLS)
        if not turn.calls:
            answer = turn.text.strip()
            break
        history.append(Msg(role="model", text=turn.text, calls=turn.calls, raw=turn.raw))
        for call in turn.calls:
            result = await tools.run(call)
            history.append(Msg(role="tool", tool_name=call.name, tool_result=result))
    else:
        answer = "Non sono riuscito a completare l’analisi in pochi passaggi: prova a riformulare la domanda in modo più specifico."

    # nodes cited in the answer are highlighted too, and unknown refs are shown as plain text
    cited = [r for r in _REF_RX.findall(answer)]
    known = {r for r in cited if g.node(r) is not None or r in g.bp}
    answer = _REF_RX.sub(lambda m: m.group(0) if m.group(1) in known else m.group(1), answer)
    highlight = list(tools.highlight)
    for r in cited:
        if r in known and r not in highlight:
            highlight.append(r)
    labels = {i: g.label(i) for i in set(highlight) | known}
    return {"answer": answer, "highlight": highlight[:40], "labels": labels, "steps": tools.steps}


# ── Gemini ───────────────────────────────────────────────────────────────────


class GeminiModel:
    """Gemini with function calling (``google-genai``). Automatic function calling is off:
    the loop above runs the tools, so every call is visible and bounded."""

    #: HTTP codes worth retrying: rate limit and temporary overload of the model
    RETRYABLE = frozenset({429, 500, 503, 504})
    DELAYS = (1.0, 3.0)

    def __init__(self, api_key: str, model: str, fallbacks: list[str] | None = None) -> None:
        if not api_key:
            raise AssistantUnavailableError(
                "Assistente non configurato: manca GEMINI_API_KEY sul backend"
            )
        from google import genai  # imported lazily: the dependency is only needed here

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._fallbacks = [m for m in (fallbacks or []) if m and m != model]
        self._sleep = asyncio.sleep
        self._current: str | None = None

    async def _generate(self, contents: list[Any], config: Any) -> Any:
        """One generation with retries on overload, then the fallback models, if any.

        A 429 (quota or rate limit of that model) moves straight to the next model: retrying
        it a second later only burns more quota. Once a model answers, the following steps of
        the same conversation start from it (its thought signatures are in the history)."""
        last: Exception | None = None
        models = [self._model, *self._fallbacks]
        if self._current in models:
            models.remove(self._current)
            models.insert(0, self._current)
        for model in models:
            for attempt in range(len(self.DELAYS) + 1):
                try:
                    resp = await self._client.aio.models.generate_content(
                        model=model, contents=contents, config=config
                    )
                    self._current = model
                    return resp
                except Exception as exc:  # noqa: BLE001 – classified below
                    last = exc
                    code = getattr(exc, "code", None)
                    if code not in self.RETRYABLE:
                        raise AssistantUnavailableError(f"Gemini non ha risposto: {exc}") from exc
                    if code == 429:
                        break
                    if attempt < len(self.DELAYS):
                        logger.warning(
                            "ea_assistant_retry", model=model, code=code, attempt=attempt + 1
                        )
                        await self._sleep(self.DELAYS[attempt])
            logger.warning("ea_assistant_model_unavailable", model=model)
        raise AssistantUnavailableError(
            "Gemini è sovraccarico in questo momento: riprova tra qualche minuto"
        ) from last

    @staticmethod
    def _contents(history: list[Msg]) -> list[Any]:
        from google.genai import types

        out: list[Any] = []
        for m in history:
            if m.role == "tool":
                part = types.Part.from_function_response(
                    name=m.tool_name, response={"result": m.tool_result}
                )
                # the Gemini API takes function results as a "user" turn; results of the
                # calls of one model turn travel together in the same turn
                prev = out[-1] if out else None
                if (
                    prev is not None
                    and prev.role == "user"
                    and prev.parts
                    and prev.parts[0].function_response
                ):
                    prev.parts.append(part)
                else:
                    out.append(types.Content(role="user", parts=[part]))
            elif m.role == "model" and m.raw is not None:
                out.append(m.raw)
            elif m.role == "model":
                parts: list[Any] = []
                if m.text:
                    parts.append(types.Part.from_text(text=m.text))
                for c in m.calls:
                    parts.append(types.Part.from_function_call(name=c.name, args=c.args))
                out.append(
                    types.Content(role="model", parts=parts or [types.Part.from_text(text="")])
                )
            else:
                out.append(types.Content(role="user", parts=[types.Part.from_text(text=m.text)]))
        return out

    async def step(self, system: str, history: list[Msg], tools: list[dict[str, Any]]) -> ModelTurn:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2,
            tools=[
                types.Tool(function_declarations=[types.FunctionDeclaration(**t) for t in tools])
            ],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        resp = await self._generate(self._contents(history), config)
        calls = [
            ToolCall(name=fc.name or "", args=dict(fc.args or {}))
            for fc in (resp.function_calls or [])
        ]
        text = ""
        if not calls:
            try:
                text = resp.text or ""
            except ValueError:
                text = ""
        raw = resp.candidates[0].content if resp.candidates else None
        return ModelTurn(text=text, calls=calls, raw=raw if calls else None)

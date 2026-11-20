"""Indice degli step (autocompletamento): frasi dei feature file e pattern behave del repo test.

`rebuild_step_index` legge tutto da Git in memoria e solo dopo sostituisce la tabella in una
transazione: nessuna chiamata di rete mentre si tengono lock di riga.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
from collections import Counter
from typing import Any, Protocol

from gherkin.parser import Parser
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.test_lifecycle import TlStepPhrase
from app.services.test_lifecycle.git_client import GitFile, TlGitError

logger = logging.getLogger(__name__)

KEYWORDS: tuple[str, ...] = ("given", "when", "then")
_TYPE_TO_KEYWORD = {"Context": "given", "Action": "when", "Outcome": "then"}
_DECORATORS = {"given", "when", "then", "step"}
_PLACEHOLDER_SPLIT = re.compile(r"(\{\{|\}\}|\{[^{}]*\})")
_FETCH_CONCURRENCY = 8


class _TreeClient(Protocol):
    async def list_tree(self, ref: str) -> list[str]: ...

    async def get_file(self, path: str, ref: str) -> GitFile | None: ...


def _steps_of(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Step di un Background/Scenario/Rule, nell'ordine del documento."""
    out: list[dict[str, Any]] = []
    for child in node.get("children", []):
        if "rule" in child:
            out.extend(_steps_of(child["rule"]))
        else:
            block = child.get("background") or child.get("scenario")
            if block:
                out.append({"__block__": True})
                out.extend(block.get("steps", []))
    return out


def extract_step_phrases(feature_text: str) -> list[tuple[str, str]]:
    """Coppie (keyword, testo) per ogni step, una per occorrenza.

    Usa `keywordType` del parser (indipendente dalla lingua); le congiunzioni (E/Ma/And/But)
    e `*` ereditano la keyword dello step precedente nello stesso blocco. Solleva se il file
    non è Gherkin valido.
    """
    doc = Parser().parse(feature_text)
    feature = doc.get("feature")
    if not feature:
        return []
    out: list[tuple[str, str]] = []
    current: str | None = None
    for step in _steps_of(feature):
        if step.get("__block__"):
            current = None
            continue
        kw = _TYPE_TO_KEYWORD.get(step.get("keywordType", ""), current)
        if kw is None:
            continue
        current = kw
        text = str(step.get("text", "")).strip()
        if text:
            out.append((kw, text))
    return out


def _decorator_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _first_literal(call: ast.Call) -> str | None:
    arg: ast.expr | None = call.args[0] if call.args else None
    if arg is None:
        for kwd in call.keywords:
            if kwd.arg in ("pattern", "step_text"):
                arg = kwd.value
                break
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _extract(py_text: str) -> list[tuple[str, str, bool]]:
    """(keyword, pattern, is_regex); `use_step_matcher("re")` cambia il formato da lì in poi."""
    try:
        tree = ast.parse(py_text)
    except (SyntaxError, ValueError):
        return []
    events: list[tuple[int, int, ast.Call]] = [
        (n.lineno, n.col_offset, n) for n in ast.walk(tree) if isinstance(n, ast.Call)
    ]
    events.sort(key=lambda e: (e[0], e[1]))
    decorator_calls = {
        id(d)
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        for d in n.decorator_list
    }
    regex = False
    out: list[tuple[str, str, bool]] = []
    for _, _, call in events:
        name = _decorator_name(call.func)
        if name == "use_step_matcher":
            mode = _first_literal(call)
            if mode is not None:
                regex = mode == "re"
        elif name in _DECORATORS and id(call) in decorator_calls:
            pattern = _first_literal(call)
            if pattern is None:
                continue
            for kw in KEYWORDS if name == "step" else (name,):
                out.append((kw, pattern, regex))
    return out


def extract_step_patterns(py_text: str) -> list[tuple[str, str]]:
    """Coppie (keyword, pattern) dai decoratori behave con stringa letterale (`@step` -> tutte)."""
    return [(kw, pattern) for kw, pattern, _ in _extract(py_text)]


def _compile(pattern: str, regex: bool) -> re.Pattern[str] | None:
    if regex:
        src = pattern
    else:
        src = "".join(
            ".+?"
            if part.startswith("{") and part not in ("{{", "}}")
            else {"{{": r"\{", "}}": r"\}"}.get(part, re.escape(part))
            for part in _PLACEHOLDER_SPLIT.split(pattern)
            if part
        )
    try:
        return re.compile(src, re.DOTALL)
    except re.error:
        return None


def pattern_matches(pattern: str, phrase: str, regex: bool = False) -> bool:
    """Match completo di una frase con un pattern behave (`parse`, oppure regex se `regex`)."""
    compiled = _compile(pattern, regex)
    return compiled is not None and compiled.fullmatch(phrase) is not None


async def _fetch(client: _TreeClient, paths: list[str], ref: str) -> dict[str, str]:
    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def one(path: str) -> tuple[str, str | None]:
        async with sem:
            try:
                f = await client.get_file(path, ref)
            except TlGitError as exc:
                logger.warning("step index: file %s saltato: %s", path, exc)
                return path, None
        return path, None if f is None else f.text

    return {p: t for p, t in await asyncio.gather(*(one(p) for p in paths)) if t is not None}


async def rebuild_step_index(db: AsyncSession, client: _TreeClient, ref: str | None = None) -> int:
    """Ricostruisce `tl_step_phrases` dal repo; ritorna il numero di frasi distinte."""
    ref = ref or settings.tl_base_branch
    tree = await client.list_tree(ref)
    feature_paths = [p for p in tree if p.startswith("src/") and p.endswith(".feature")]
    py_paths = [p for p in tree if p.startswith("src/") and p.endswith(".py") and "/steps/" in p]
    features = await _fetch(client, feature_paths, ref)
    modules = await _fetch(client, py_paths, ref)

    counts: Counter[tuple[str, str]] = Counter()
    for path, text in features.items():
        try:
            counts.update(extract_step_phrases(text))
        except Exception as exc:  # noqa: BLE001 - un file non valido non blocca l'indice
            logger.warning("step index: feature %s non analizzabile: %s", path, exc)

    patterns: dict[str, list[re.Pattern[str]]] = {kw: [] for kw in KEYWORDS}
    for text in modules.values():
        for kw, pattern, is_regex in _extract(text):
            compiled = _compile(pattern, is_regex)
            if compiled is not None:
                patterns[kw].append(compiled)

    rows = [
        TlStepPhrase(
            keyword=kw,
            text=text,
            occurrences=n,
            implemented=any(p.fullmatch(text) for p in patterns[kw]),
        )
        for (kw, text), n in counts.items()
    ]
    await db.execute(delete(TlStepPhrase))
    db.add_all(rows)
    await db.commit()
    return len(rows)

"""Sinossi della feature generata con Gemini, a partire dal testo dell'"Anteprima file".

Regole portate da `docs/migrazione-generazione-sinossi.md` (pulizia del testo, limite lineare di
parole, prompt, validazione, un secondo tentativo più stretto). La chiave Gemini è quella del
backend (`GEMINI_API_KEY`): non esce mai dal server. Il testo non viene anonimizzato: finiscono
nel prompt anche valori di tabella e docstring.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.schemas.test_lifecycle import TlSynopsisOut
from app.services.ea.assistant import AssistantUnavailableError, GeminiModel
from app.services.test_lifecycle import reviews

logger = structlog.get_logger(__name__)

MAX_WORDS_CAP = 80
DOCSTRING_DELIMITERS = ('"""', "```")
LABEL_PATTERN = re.compile(r"^\*{0,2}sinossi\*{0,2}\s*:\*{0,2}\s*", re.IGNORECASE)
NUMBERED_LINE_PATTERN = re.compile(r"\d+\.")
LIST_MARKERS = ("-", "*", "#")
SCENARIO_PATTERN = re.compile(
    r"^\s*(?:scenario(?:\s+(?:outline|template))?|schema\s+dello\s+scenario|esempio|example)\s*:",
    re.IGNORECASE,
)
QUOTE_PAIRS = {'"': '"', "'": "'", "«": "»", "“": "”", "‘": "’", "`": "`"}

PROMPT_TEMPLATE = (
    "Sei un analista QA. Di seguito trovi il contenuto di un feature file Gherkin, già privo di "
    "tag e commenti.\n"
    "Scrivi una sinossi descrittiva dell'intero feature file, non scenario per scenario.\n"
    "Regole: al massimo {max_words} parole; un solo paragrafo di testo semplice, senza elenchi, "
    "titoli, markdown o virgolette; descrivi che cosa verifica la funzionalità nel suo insieme, in "
    "quale contesto e con quali esiti attesi, trattando in modo aggregato i casi positivi e "
    "negativi; non elencare gli scenari, non citare gli step alla lettera, né identificativi, nomi "
    "di tag, valori di test o dati sensibili; scrivi in italiano anche se il file è in inglese; "
    "rispondi solo con la sinossi, senza preambolo.\n\n"
    "Feature file:\n{feature_text}\n"
)


# Segnaposto classico, entro il minimo di 30 parole del limite P(n).
PLACEHOLDER_SYNOPSIS = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor "
    "incididunt ut labore et dolore magna aliqua."
)


class SynopsisError(RuntimeError):
    """Errore di dominio: `code` e `status` guidano la risposta HTTP."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class SynopsisRejectedError(ValueError):
    """La risposta del modello non rispetta le regole di formato."""


# ── pulizia e conteggio ───────────────────────────────────────────────────────


def _docstring_flags(lines: list[str]) -> list[bool]:
    flags: list[bool] = []
    open_delimiter: str | None = None
    for line in lines:
        stripped = line.strip()
        if open_delimiter is not None:
            flags.append(True)
            if stripped.startswith(open_delimiter):
                open_delimiter = None
            continue
        for delimiter in DOCSTRING_DELIMITERS:
            if stripped.startswith(delimiter):
                flags.append(True)
                open_delimiter = None if delimiter in stripped[len(delimiter) :] else delimiter
                break
        else:
            flags.append(False)
    return flags


def clean_feature_text(raw: str) -> str:
    """Testo inviato al modello: senza righe di tag e commento (fuori dalle docstring), righe
    vuote ridotte a una, spazi finali tolti, fine riga LF."""
    text = raw[1:] if raw.startswith("﻿") else raw
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept = [
        line.rstrip()
        for line, in_doc in zip(lines, _docstring_flags(lines), strict=True)
        if in_doc or not line.lstrip().startswith(("#", "@"))
    ]
    collapsed: list[str] = []
    for line in kept:
        if line == "" and (not collapsed or collapsed[-1] == ""):
            continue
        collapsed.append(line)
    while collapsed and collapsed[-1] == "":
        collapsed.pop()
    return "\n".join(collapsed)


def count_scenarios(cleaned: str) -> int:
    """Definizioni di scenario fuori dalle docstring; uno Scenario Outline vale uno."""
    lines = cleaned.split("\n")
    return sum(
        1
        for line, in_doc in zip(lines, _docstring_flags(lines), strict=True)
        if not in_doc and SCENARIO_PATTERN.match(line)
    )


def max_words_for(scenario_count: int) -> int:
    """P(n) = min(80, floor(30 + 50/11 * (max(1, n) - 1))), in aritmetica intera."""
    if scenario_count < 0:
        raise ValueError("il numero di scenari non può essere negativo")
    return min(MAX_WORDS_CAP, (330 + 50 * (max(1, scenario_count) - 1)) // 11)


def build_prompt(cleaned: str, max_words: int) -> str:
    return PROMPT_TEMPLATE.replace("{max_words}", str(max_words)).replace("{feature_text}", cleaned)


def retry_instruction(max_words: int) -> str:
    return (
        "Il tentativo precedente superava il limite o il formato: scrivi al massimo "
        f"{max(20, max_words - 10)} parole in un solo paragrafo, senza elenchi."
    )


# ── validazione ───────────────────────────────────────────────────────────────


def _unwrap_lines(raw: str) -> list[str]:
    text = LABEL_PATTERN.sub("", raw.strip(), count=1).strip()
    if len(text) >= 2 and text[0] in QUOTE_PAIRS and text[-1] == QUOTE_PAIRS[text[0]]:
        text = text[1:-1].strip()
    return [line.strip() for line in text.splitlines() if line.strip()]


def validate_synopsis(raw: str, max_words: int) -> str:
    """Sinossi su una riga, oppure `SynopsisRejectedError`. Il testo non è mai troncato."""
    lines = _unwrap_lines(raw)
    if not lines:
        raise SynopsisRejectedError("risposta vuota")
    for line in lines:
        if line.startswith(LIST_MARKERS) or NUMBERED_LINE_PATTERN.match(line):
            raise SynopsisRejectedError("la risposta contiene un elenco o un titolo")
    if any("@" in line for line in lines):
        raise SynopsisRejectedError("la risposta contiene '@'")
    text = " ".join(" ".join(lines).split())
    words = len(text.split())
    if words > max_words:
        raise SynopsisRejectedError(f"la risposta ha {words} parole, il limite è {max_words}")
    return text


# ── generazione ───────────────────────────────────────────────────────────────


@dataclass
class _Generated:
    synopsis: str
    model: str


def _gemini() -> GeminiModel:
    fallbacks = [m.strip() for m in settings.gemini_fallback_models.split(",") if m.strip()]
    return GeminiModel(settings.gemini_api_key, settings.tl_synopsis_model, fallbacks)


async def _generate(prompt: str, max_words: int) -> _Generated:
    """Una chiamata, più un secondo tentativo con prompt più stretto se la risposta non è valida."""
    try:
        model = _gemini()
        for attempt in range(2):
            full = prompt if attempt == 0 else f"{prompt}\n{retry_instruction(max_words)}\n"
            answer = await model.generate_text(full)
            try:
                return _Generated(validate_synopsis(answer, max_words), model.model_name)
            except SynopsisRejectedError as exc:
                logger.warning("tl_synopsis_rejected", attempt=attempt + 1, reason=str(exc))
    except AssistantUnavailableError as exc:
        raise SynopsisError("synopsis_unavailable", str(exc), 502) from exc
    raise SynopsisError(
        "synopsis_invalid",
        "Gemini non ha prodotto una sinossi valida: riprova o scrivila a mano",
        502,
    )


async def generate(
    db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID
) -> TlSynopsisOut:
    """Genera la sinossi dal feature file mostrato nel tab "Anteprima file" della revisione."""
    preview = await reviews.preview(db, feature_id, revision_id)
    if preview.errors:
        raise SynopsisError(
            "preview_has_errors",
            "L'anteprima del file contiene errori: correggili prima di generare la sinossi",
            422,
        )
    cleaned = clean_feature_text(preview.file)
    if not cleaned:
        raise SynopsisError("feature_empty", "Il feature file è vuoto", 422)
    max_words = max_words_for(count_scenarios(cleaned))
    if not settings.gemini_api_key:
        # Senza chiave (tipicamente in locale) nessuna chiamata: testo segnaposto.
        return TlSynopsisOut(
            synopsis=PLACEHOLDER_SYNOPSIS, max_words=max_words, model="placeholder"
        )
    result = await _generate(build_prompt(cleaned, max_words), max_words)
    return TlSynopsisOut(synopsis=result.synopsis, max_words=max_words, model=result.model)

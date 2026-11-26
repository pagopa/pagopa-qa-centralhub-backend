"""Codici, validazione e rendering Gherkin del Test Lifecycle (funzioni pure, nessun DB)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from gherkin.errors import CompositeParserException, ParserError
from gherkin.parser import Parser

from app.models.test_lifecycle import TlPolarity, TlScenarioKind

# Polarity tags: the file standard is English, the Italian spelling is still read.
POLARITY_TAG: dict[TlPolarity, str] = {
    TlPolarity.POSITIVO: "positive",
    TlPolarity.NEGATIVO: "negative",
}
POLARITY_BY_TAG: dict[str, TlPolarity] = {
    "positive": TlPolarity.POSITIVO,
    "positivo": TlPolarity.POSITIVO,
    "negative": TlPolarity.NEGATIVO,
    "negativo": TlPolarity.NEGATIVO,
}

# Parole chiave di sezione italiane (gherkin-official, lingua "it")
_SECTION_RE = re.compile(
    r"^(Funzionalit[àa]|Esigenza di Business|Abilit[àa]|Contesto|Esempio|Scenario"
    r"|Schema dello scenario|Esempi|Regola)\s*:",
    re.IGNORECASE,
)
_NO_KEYWORD_MSG = "Riga non valida: usa una parola chiave italiana (Dato, Quando, E, Ma)."
_STEP_RE = re.compile(r"^(Dato|Data|Dati|Date|Quando|Allora|E|Ed|Ma|\*)\s+\S")
_BACKGROUND_RE = re.compile(r"^(Dato|Data|Dati|Date|E)\b")
_PLACEHOLDER_RE = re.compile(r"<([^<>\s|]+)>")

# righe del mini-documento che precedono il blocco step:
# "# language: it", "Funzionalità: x", "Scenario:/Contesto:"
_HEADER_LINES = 3


def feature_code(scope_code: str, number: int) -> str:
    return f"FEAT_{number:03d}_{scope_code}"


def feature_tag(scope_code: str, number: int) -> str:
    return f"@{scope_code.upper()}_{number:03d}"


def scenario_tag(scope_code: str, feature_number: int, scenario_number: int) -> str:
    return f"@{scope_code.upper()}_{feature_number:03d}_{scenario_number:02d}"


@dataclass(frozen=True)
class GherkinError:
    line: int
    message: str


def _example_table(examples: dict[str, Any] | None) -> tuple[list[str], list[list[str]]]:
    if not examples:
        return [], []
    columns = [str(c) for c in (examples.get("headers") or [])]
    rows = [[str(c) for c in row] for row in (examples.get("rows") or [])]
    return columns, rows


def _escape_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n")


def _table_lines(columns: list[str], rows: list[list[str]], indent: str) -> list[str]:
    cells = [[_escape_cell(c) for c in columns]] + [[_escape_cell(c) for c in r] for r in rows]
    widths = [max(len(r[i]) if i < len(r) else 0 for r in cells) for i in range(len(columns))]
    out = []
    for r in cells:
        padded = [(r[i] if i < len(r) else "").ljust(widths[i]) for i in range(len(columns))]
        out.append(indent + "| " + " | ".join(padded) + " |")
    return out


def _parser_errors(exc: CompositeParserException) -> list[ParserError]:
    return list(exc.errors)


def validate_steps(
    steps: str,
    *,
    kind: TlScenarioKind,
    examples: dict[str, Any] | None,
    background: bool = False,
) -> list[GherkinError]:
    """Valida un blocco di step. Le righe degli errori sono relative al blocco (da 1)."""
    lines = steps.split("\n")
    if not any(line.strip() for line in lines):
        return [GherkinError(1, "Gli step non possono essere vuoti.")]

    errors: list[GherkinError] = []
    columns, rows = _example_table(examples)
    is_outline = kind == TlScenarioKind.OUTLINE and not background

    step_count = 0
    in_docstring = False
    for i, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text:
            continue
        if text.startswith(('"""', "```")):
            in_docstring = not in_docstring
            if step_count == 0:
                errors.append(GherkinError(i, _NO_KEYWORD_MSG))
            continue
        if in_docstring:
            continue
        if text.startswith("|") and step_count > 0:
            continue
        if text.startswith("#"):
            errors.append(GherkinError(i, "I commenti non sono ammessi negli step."))
        elif text.startswith("@"):
            errors.append(GherkinError(i, "I tag non sono ammessi negli step."))
        elif _SECTION_RE.match(text):
            errors.append(
                GherkinError(
                    i,
                    "Le parole chiave di sezione (Scenario, Esempi...) non sono ammesse.",
                )
            )
        elif background and not _BACKGROUND_RE.match(text):
            errors.append(
                GherkinError(
                    i,
                    "Nel Contesto sono ammessi solo step che iniziano con Dato/Data/Dati/Date o E.",
                )
            )
        elif not _STEP_RE.match(text):
            errors.append(GherkinError(i, _NO_KEYWORD_MSG))
        else:
            step_count += 1

    if step_count == 0 and not errors:
        errors.append(GherkinError(1, "Servono almeno uno step."))

    if is_outline:
        if not columns or not rows:
            errors.append(
                GherkinError(
                    1, "Uno Schema dello scenario richiede una tabella Esempi con almeno una riga."
                )
            )
        elif any(len(r) != len(columns) for r in rows):
            errors.append(
                GherkinError(1, "Ogni riga degli Esempi deve avere tante celle quante le colonne.")
            )
        else:
            seen: set[tuple[int, str]] = set()
            for i, raw in enumerate(lines, start=1):
                for name in _PLACEHOLDER_RE.findall(raw):
                    if name not in columns and (i, name) not in seen:
                        seen.add((i, name))
                        errors.append(
                            GherkinError(
                                i,
                                f"Il segnaposto <{name}> non ha una colonna negli Esempi.",
                            )
                        )

    if errors:
        return sorted(errors, key=lambda e: e.line)

    doc = ["# language: it", "Funzionalità: x"]
    if background:
        doc.append("  Contesto:")
    else:
        doc.append("  Schema dello scenario: x" if is_outline else "  Scenario: x")
    doc.extend(("    " + line.strip()) if line.strip() else "" for line in lines)
    if is_outline:
        doc.append("")
        doc.append("    Esempi:")
        doc.extend(_table_lines(columns, rows, "      "))
    try:
        Parser().parse("\n".join(doc) + "\n")
    except CompositeParserException as exc:
        seen_lines: set[int] = set()
        for err in _parser_errors(exc):
            line = err.location.get("line", 1) - _HEADER_LINES
            if line < 1 or line > len(lines):
                line = 1  # errore nella tabella Esempi: riportato sulla riga 1
            if line in seen_lines:
                continue
            seen_lines.add(line)
            errors.append(
                GherkinError(
                    line,
                    "Riga non valida: usa una parola chiave italiana (Dato, Quando, E, Ma).",
                )
            )
    return errors


@dataclass
class RenderScenario:
    tag: str
    title: str
    kind: TlScenarioKind
    polarity: TlPolarity
    extra_tags: list[str]
    steps: str
    examples: dict[str, Any] | None
    detail_link: str | None


@dataclass
class RenderFeature:
    code: str
    feature_tag: str
    title: str
    description: str | None
    background_steps: str | None
    scenarios: list[RenderScenario] = field(default_factory=list)
    test_type: str | None = None


def _single_line(value: str | None) -> str:
    """Prima riga non vuota, ripulita: un valore su più righe non deve iniettare Gherkin."""
    if not value:
        return ""
    return next((ln.strip() for ln in value.splitlines() if ln.strip()), "")


def _indent_block(text: str, indent: str) -> list[str]:
    return [
        (indent + line.strip()) if line.strip() else "" for line in text.strip("\n").split("\n")
    ]


def render_feature_file(feature: RenderFeature) -> str:
    out = [
        f"# qa-hub: {feature.code}",
        "# language: it",
        f"{feature.feature_tag} @{feature.test_type}" if feature.test_type else feature.feature_tag,
        f"Funzionalità: {feature.title}",
    ]
    if feature.description and feature.description.strip():
        out.extend(_indent_block(feature.description, "  "))
    if feature.background_steps and feature.background_steps.strip():
        out.extend(["", "  Contesto:"])
        out.extend(_indent_block(feature.background_steps, "    "))
    for sc in feature.scenarios:
        out.append("")
        tags = [f"@{t}" for t in sc.extra_tags] + [f"@{POLARITY_TAG[sc.polarity]}"]
        out.append("  " + " ".join(tags))
        out.append(f"  {sc.tag}")
        link = _single_line(sc.detail_link)
        if link:
            out.append(f"  #link:{link}")
        is_outline = sc.kind == TlScenarioKind.OUTLINE
        out.append(f"  {'Schema dello scenario' if is_outline else 'Scenario'}: {sc.title}")
        out.extend(_indent_block(sc.steps, "    "))
        if is_outline:
            columns, rows = _example_table(sc.examples)
            if columns:
                out.extend(["", "    Esempi:"])
                out.extend(_table_lines(columns, rows, "      "))
    return "\n".join(out) + "\n"

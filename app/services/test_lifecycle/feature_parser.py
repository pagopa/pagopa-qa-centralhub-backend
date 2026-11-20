"""Parser dei feature file gestiti e classificazione delle differenze (funzioni pure, nessun DB)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from gherkin.errors import CompositeParserException
from gherkin.parser import Parser

from app.models.test_lifecycle import TlPolarity, TlScenarioKind
from app.services.test_lifecycle.gherkin import (
    ALLOWED_EXTRA_TAGS,
    RenderFeature,
    render_feature_file,
)

_IDENTITY_RE = re.compile(r"^@[A-Z0-9]+_\d{3}_\d{2}$")
_CODE_RE = re.compile(r"^#\s*qa-hub:\s*(\S+)\s*$")
_LINK_PREFIX = "#link:"


def normalize_text(text: str) -> str:
    """Rimuove il BOM, CRLF -> LF, spazi a fine riga, una sola newline finale."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    return "\n".join(lines).rstrip("\n") + "\n"


@dataclass
class ParsedScenario:
    identity_tag: str | None
    all_tags: list[str]
    title: str
    kind: TlScenarioKind
    polarity: TlPolarity | None
    extra_tags: list[str]
    steps: str
    examples: dict[str, Any] | None
    detail_link: str | None
    line: int


@dataclass
class ParsedFeature:
    code: str | None
    feature_tag: str | None
    title: str = ""
    description: str | None = None
    background_steps: str | None = None
    scenarios: list[ParsedScenario] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StructuralChange:
    kind: str
    scenario_tag: str | None
    message: str


@dataclass
class DiffResult:
    textual: list[str] = field(default_factory=list)
    structural: list[StructuralChange] = field(default_factory=list)
    new_scenarios: list[ParsedScenario] = field(default_factory=list)


def _read_code(text: str) -> str | None:
    for line in text.split("\n")[:5]:
        m = _CODE_RE.match(line.strip())
        if m:
            return m.group(1)
    return None


def _steps_text(steps: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for st in steps:
        out.append(f"{st['keyword']}{st['text']}".strip())
        doc = st.get("docString")
        if doc:
            delim = doc.get("delimiter", '"""')
            out.extend([delim, *doc["content"].split("\n"), delim])
        table = st.get("dataTable")
        if table:
            for row in table["rows"]:
                out.append("| " + " | ".join(_esc(c["value"]) for c in row["cells"]) + " |")
    return "\n".join(out)


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n")


def _examples(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not blocks:
        return None
    block = blocks[0]
    header = block.get("tableHeader")
    if not header:
        return None
    return {
        "headers": [c["value"] for c in header["cells"]],
        "rows": [[c["value"] for c in r["cells"]] for r in block.get("tableBody", [])],
    }


def _description(text: str | None) -> str | None:
    if not text or not text.strip():
        return None
    return "\n".join(ln.strip() for ln in text.strip("\n").split("\n"))


def parse_feature_file(text: str) -> ParsedFeature:
    text = normalize_text(text)
    code = _read_code(text)
    try:
        doc = Parser().parse(text)
    except CompositeParserException as exc:
        msgs = [f"Riga {e.location.get('line', '?')}: {e}" for e in exc.errors]
        return ParsedFeature(code=code, feature_tag=None, errors=msgs or [str(exc)])
    except Exception as exc:  # noqa: BLE001 - qualunque errore del parser diventa un errore di parsing
        return ParsedFeature(code=code, feature_tag=None, errors=[str(exc)])

    feature = doc.get("feature")
    if not feature:
        return ParsedFeature(code=code, feature_tag=None, errors=["Nessuna funzionalità trovata."])

    links = {
        c["location"]["line"]: c["text"].strip()[len(_LINK_PREFIX) :].strip()
        for c in doc.get("comments", [])
        if c["text"].strip().startswith(_LINK_PREFIX)
    }
    ftags = [t["name"] for t in feature.get("tags", [])]
    parsed = ParsedFeature(
        code=code,
        feature_tag=ftags[0] if ftags else None,
        title=feature["name"],
        description=_description(feature.get("description")),
    )
    for child in feature.get("children", []):
        if "rule" in child:
            return ParsedFeature(
                code=code,
                feature_tag=parsed.feature_tag,
                errors=["Le regole (Regola:) non sono supportate: usa solo Scenario e Contesto."],
            )
        if "scenario" in child and len(child["scenario"].get("examples", [])) > 1:
            return ParsedFeature(
                code=code,
                feature_tag=parsed.feature_tag,
                errors=[
                    f"Riga {child['scenario']['location']['line']}: "
                    "è ammessa una sola tabella Esempi per scenario."
                ],
            )
        if "background" in child:
            parsed.background_steps = _steps_text(child["background"]["steps"]) or None
        elif "scenario" in child:
            parsed.scenarios.append(_parse_scenario(child["scenario"], links))
    return parsed


def _parse_scenario(sc: dict[str, Any], links: dict[int, str]) -> ParsedScenario:
    tags = [t["name"] for t in sc.get("tags", [])]
    identity = next((t for t in tags if _IDENTITY_RE.match(t)), None)
    polarity = None
    extra: list[str] = []
    for t in tags:
        name = t.lstrip("@")
        if name in (TlPolarity.POSITIVO.value, TlPolarity.NEGATIVO.value):
            polarity = TlPolarity(name)
        elif name in ALLOWED_EXTRA_TAGS:
            extra.append(name)
    line = sc["location"]["line"]
    tag_lines = [t["location"]["line"] for t in sc.get("tags", [])]
    first = min(tag_lines) if tag_lines else line
    link = next((links[n] for n in range(first, line) if n in links), None)
    is_outline = (
        bool(sc.get("examples"))
        or "outline" in sc["keyword"].lower()
        or "schema" in sc["keyword"].lower()
    )
    return ParsedScenario(
        identity_tag=identity,
        all_tags=tags,
        title=sc["name"],
        kind=TlScenarioKind.OUTLINE if is_outline else TlScenarioKind.SCENARIO,
        polarity=polarity,
        extra_tags=extra,
        steps=_steps_text(sc["steps"]),
        examples=_examples(sc.get("examples", [])),
        detail_link=link,
        line=line,
    )


def _norm(value: str | None) -> str:
    return (value or "").strip()


def _diff_scenario(exp: ParsedScenario, got: ParsedScenario) -> list[str]:
    tag = exp.identity_tag
    out: list[str] = []
    if _norm(exp.title) != _norm(got.title):
        out.append(f"{tag}: titolo modificato.")
    if exp.kind != got.kind:
        out.append(f"{tag}: tipo di scenario modificato.")
    if exp.polarity != got.polarity:
        out.append(f"{tag}: polarità modificata.")
    if sorted(exp.extra_tags) != sorted(got.extra_tags):
        out.append(f"{tag}: tag aggiuntivi modificati.")
    if _norm(exp.steps) != _norm(got.steps):
        out.append(f"{tag}: step modificati.")
    if exp.kind == TlScenarioKind.OUTLINE and (exp.examples or None) != (got.examples or None):
        out.append(f"{tag}: esempi modificati.")
    if _norm(exp.detail_link) != _norm(got.detail_link):
        out.append(f"{tag}: link di dettaglio modificato.")
    return out


def _canonical(expected: RenderFeature) -> ParsedFeature:
    """Il lato atteso passa dallo stesso render + parse del file reale: stessa normalizzazione."""
    canon = parse_feature_file(render_feature_file(expected))
    if canon.errors or len(canon.scenarios) != len(expected.scenarios):
        # contenuto atteso non renderizzabile: si confronta il testo grezzo normalizzato
        canon = ParsedFeature(
            code=expected.code,
            feature_tag=expected.feature_tag,
            title=expected.title,
            description=expected.description,
            background_steps=expected.background_steps,
            scenarios=[
                ParsedScenario(
                    identity_tag=sc.tag,
                    all_tags=[sc.tag],
                    title=sc.title,
                    kind=sc.kind,
                    polarity=sc.polarity,
                    extra_tags=list(sc.extra_tags),
                    steps=sc.steps,
                    examples=sc.examples,
                    detail_link=sc.detail_link,
                    line=0,
                )
                for sc in expected.scenarios
            ],
        )
    else:
        for ps, sc in zip(canon.scenarios, expected.scenarios, strict=True):
            ps.identity_tag = sc.tag
    return canon


def classify(
    expected: RenderFeature,
    parsed: ParsedFeature,
    *,
    scope_prefix: str,
    feature_number: int,
) -> DiffResult:
    result = DiffResult()
    structural = result.structural

    if parsed.code is None:
        structural.append(
            StructuralChange("header_missing", None, "Intestazione `# qa-hub: <codice>` assente.")
        )
    elif parsed.code != expected.code:
        structural.append(
            StructuralChange(
                "header_changed",
                None,
                f"Intestazione modificata: atteso {expected.code}, trovato {parsed.code}.",
            )
        )
    if parsed.feature_tag != expected.feature_tag:
        structural.append(
            StructuralChange(
                "feature_tag_changed",
                None,
                f"Tag della funzionalità modificato: atteso {expected.feature_tag}, "
                f"trovato {parsed.feature_tag or 'nessuno'}.",
            )
        )

    canon = _canonical(expected)
    if _norm(canon.title) != _norm(parsed.title):
        result.textual.append("Titolo della funzionalità modificato.")
    if _norm(canon.description) != _norm(parsed.description):
        result.textual.append("Descrizione modificata.")
    if _norm(canon.background_steps) != _norm(parsed.background_steps):
        result.textual.append("Contesto modificato.")

    prefix_re = re.compile(rf"^@{re.escape(scope_prefix.upper())}_{feature_number:03d}_\d{{2}}$")
    known = {s.identity_tag: s for s in canon.scenarios if s.identity_tag}
    seen: set[str] = set()
    for sc in parsed.scenarios:
        tag = sc.identity_tag
        if tag is None:
            structural.append(
                StructuralChange(
                    "missing_tag",
                    None,
                    f"Riga {sc.line}: lo scenario «{sc.title}» non ha un tag identità valido.",
                )
            )
            continue
        identities = [t for t in sc.all_tags if _IDENTITY_RE.match(t)]
        if len(identities) > 1:
            structural.append(
                StructuralChange(
                    "duplicate_identity",
                    tag,
                    f"Riga {sc.line}: lo scenario «{sc.title}» ha più tag identità "
                    f"({', '.join(identities)}).",
                )
            )
        if not prefix_re.match(tag):
            structural.append(
                StructuralChange(
                    "wrong_tag",
                    tag,
                    f"Riga {sc.line}: il tag {tag} non appartiene a questa funzionalità "
                    f"(atteso @{scope_prefix.upper()}_{feature_number:03d}_NN).",
                )
            )
            continue
        if tag in seen:
            structural.append(
                StructuralChange(
                    "duplicate_tag", tag, f"Riga {sc.line}: il tag {tag} è usato più volte."
                )
            )
            continue
        seen.add(tag)
        if tag in known:
            result.textual.extend(_diff_scenario(known[tag], sc))
        else:
            result.new_scenarios.append(sc)

    for tag in known:
        if tag not in seen:
            structural.append(
                StructuralChange("removed_scenario", tag, f"Lo scenario {tag} è stato rimosso.")
            )
    return result

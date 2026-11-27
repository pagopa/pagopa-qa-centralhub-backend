"""Test del modulo Gherkin del Test Lifecycle (codici, validazione, rendering)."""

from gherkin.parser import Parser

from app.models.test_lifecycle import TlPolarity, TlScenarioKind
from app.services.test_lifecycle.gherkin import (
    RenderFeature,
    RenderScenario,
    feature_code,
    feature_tag,
    render_feature_file,
    scenario_tag,
    validate_steps,
)

S = TlScenarioKind.SCENARIO
OUT = TlScenarioKind.OUTLINE


def test_codes_and_tags():
    assert feature_code("FdR3", 1) == "FEAT_001_FdR3"
    assert feature_tag("FdR3", 1) == "@FDR3_001"
    assert scenario_tag("FdR3", 1, 2) == "@FDR3_001_02"


def test_validate_steps_accepts_italian_keywords():
    steps = "Dato che sono loggato\nE ho un carrello\nQuando pago\nAllora vedo la conferma"
    assert validate_steps(steps, kind=S, examples=None) == []


def test_validate_steps_rejects_line_without_keyword():
    errs = validate_steps("Dato un utente\nil sistema risponde", kind=S, examples=None)
    assert [e.line for e in errs] == [2]
    assert errs[0].message


def test_validate_steps_rejects_section_keyword_inside_steps():
    errs = validate_steps("Dato un utente\nScenario: x", kind=S, examples=None)
    assert [e.line for e in errs] == [2]


def test_validate_steps_rejects_comment_and_tag_lines():
    errs = validate_steps("Dato un utente\n# nota\n@tag\nQuando agisce", kind=S, examples=None)
    assert [e.line for e in errs] == [2, 3]


def test_validate_steps_rejects_empty_steps():
    errs = validate_steps("  \n\n", kind=S, examples=None)
    assert len(errs) == 1 and errs[0].line == 1


def test_validate_steps_outline_requires_examples():
    errs = validate_steps("Dato <a>", kind=OUT, examples=None)
    assert errs and "Esempi" in errs[0].message
    errs = validate_steps("Dato <a>", kind=OUT, examples={"headers": ["a"], "rows": []})
    assert errs


def test_validate_steps_outline_placeholder_without_column():
    errs = validate_steps(
        "Dato un utente\nQuando chiama <flowid>",
        kind=OUT,
        examples={"headers": ["altro"], "rows": [["1"]]},
    )
    assert [e.line for e in errs] == [2]
    assert "flowid" in errs[0].message


def test_validate_steps_outline_valid():
    ex = {"headers": ["a", "b"], "rows": [["1", "2"], ["3", "4"]]}
    assert validate_steps("Dato <a>\nQuando <b>", kind=OUT, examples=ex) == []


def test_validate_steps_background_allows_only_given_and_and():
    assert (
        validate_steps("Dati un utente\nE un carrello", kind=S, examples=None, background=True)
        == []
    )
    errs = validate_steps("Dato un utente\nQuando agisce", kind=S, examples=None, background=True)
    assert [e.line for e in errs] == [2]


def test_validate_steps_rejects_non_keyword_first_line_and_zero_steps():
    assert [e.line for e in validate_steps("il sistema risponde", kind=S, examples=None)] == [1]
    assert [e.line for e in validate_steps("testo libero\nDato a", kind=S, examples=None)] == [1]
    assert [e.line for e in validate_steps("Dato", kind=S, examples=None)] == [1]


def test_validate_steps_rejects_all_italian_section_keywords():
    for kw in ("Esempio", "Esempi", "Regola", "Contesto", "Abilità", "Esigenza di Business"):
        errs = validate_steps(f"Dato a\n{kw}: b\nQuando c", kind=S, examples=None)
        assert [e.line for e in errs] == [2], kw


def test_validate_steps_examples_table_error_on_line_1():
    ex = {"headers": ["a", "b"], "rows": [["1"]]}
    errs = validate_steps("Dato <a>\nQuando <b>\nAllora ok", kind=OUT, examples=ex)
    assert [e.line for e in errs] == [1]


GOLDEN = """\
# qa-hub: FEAT_001_FdR3
# language: it
@FDR3_001
Funzionalità: Pagamento
  Descrizione della funzionalità.

  Contesto:
    Dati un utente autenticato

  @runnable
  @positive
  @FDR3_001_01
  #link:req-1|Requisito
  Scenario: Paga con successo
    Dato un carrello
    Quando paga
    Allora vede la conferma

  @negative
  @FDR3_001_02
  Schema dello scenario: Paga con errore
    Dato un flusso <flowid>
    Allora riceve <esito>

    Esempi:
      | flowid | esito   |
      | a1     | errore  |
      | b22    | rifiuto |
"""


def _feature() -> RenderFeature:
    return RenderFeature(
        code="FEAT_001_FdR3",
        feature_tag="@FDR3_001",
        title="Pagamento",
        description="Descrizione della funzionalità.",
        background_steps="Dati un utente autenticato",
        scenarios=[
            RenderScenario(
                tag="@FDR3_001_01",
                title="Paga con successo",
                kind=S,
                polarity=TlPolarity.POSITIVO,
                extra_tags=["runnable"],
                steps="Dato un carrello\nQuando paga\nAllora vede la conferma",
                examples=None,
                detail_link="req-1|Requisito",
            ),
            RenderScenario(
                tag="@FDR3_001_02",
                title="Paga con errore",
                kind=OUT,
                polarity=TlPolarity.NEGATIVO,
                extra_tags=[],
                steps="Dato un flusso <flowid>\nAllora riceve <esito>",
                examples={
                    "headers": ["flowid", "esito"],
                    "rows": [["a1", "errore"], ["b22", "rifiuto"]],
                },
                detail_link=None,
            ),
        ],
    )


def test_render_feature_file_matches_golden():
    assert render_feature_file(_feature()) == GOLDEN


def test_rendered_file_parses_with_gherkin_parser():
    doc = Parser().parse(render_feature_file(_feature()))
    assert len(doc["feature"]["children"]) == 3


def test_render_writes_the_test_type_after_the_feature_tag():
    feature = _feature()
    feature.test_type = "integration"
    assert render_feature_file(feature) == GOLDEN.replace(
        "@FDR3_001\n", "@FDR3_001\n@integration\n", 1
    )


def test_render_puts_every_tag_on_its_own_line():
    feature = _feature()
    feature.test_type = "integration"
    for line in render_feature_file(feature).splitlines():
        if line.strip().startswith("@"):
            assert len(line.split()) == 1, line


def test_render_without_test_type_is_unchanged():
    feature = _feature()
    assert feature.test_type is None
    assert render_feature_file(feature) == GOLDEN

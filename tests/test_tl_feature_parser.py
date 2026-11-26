from app.models.test_lifecycle import TlPolarity, TlScenarioKind
from app.services.test_lifecycle.feature_parser import (
    classify,
    normalize_text,
    parse_feature_file,
)
from app.services.test_lifecycle.gherkin import render_feature_file
from tests.test_tl_gherkin import GOLDEN, _feature

KW = {"scope_prefix": "FDR3", "feature_number": 1}


def _expected():
    return _feature()


def _parsed(text=None):
    return parse_feature_file(text if text is not None else GOLDEN)


def test_roundtrip_render_parse():
    exp = _expected()
    p = parse_feature_file(render_feature_file(exp))
    assert p.errors == []
    assert p.code == exp.code
    assert p.feature_tag == exp.feature_tag
    assert p.title == exp.title
    assert p.description == exp.description
    assert p.background_steps == exp.background_steps
    assert len(p.scenarios) == 2
    for ps, es in zip(p.scenarios, exp.scenarios, strict=True):
        assert ps.identity_tag == es.tag
        assert ps.title == es.title
        assert ps.kind == es.kind
        assert ps.polarity == es.polarity
        assert ps.extra_tags == es.extra_tags
        assert ps.steps == es.steps
        assert ps.examples == es.examples
        assert ps.detail_link == es.detail_link
    assert p.scenarios[0].line > 0


def test_roundtrip_escaped_cells():
    exp = _expected()
    exp.scenarios[1].examples = {
        "headers": ["flowid", "esito"],
        "rows": [["a|b", "x\\y"], ["l1\nl2", "ok"]],
    }
    p = parse_feature_file(render_feature_file(exp))
    assert p.scenarios[1].examples == exp.scenarios[1].examples


def test_parse_normalizes_crlf_bom_trailing_spaces():
    messy = "\ufeff" + GOLDEN.replace("\n", "   \r\n") + "\n\n"
    assert normalize_text(messy) == GOLDEN
    p = parse_feature_file(messy)
    assert p.errors == []
    assert p.code == "FEAT_001_FdR3"
    assert classify(_expected(), p, **KW).textual == []


def test_parse_error_returns_errors():
    p = parse_feature_file(
        "# qa-hub: FEAT_001_FdR3\n# language: it\nFunzionalità: x\n"
        "  Scenario: a\n    Dato b\n  Funzionalità: y\n"
    )
    assert p.errors
    assert p.scenarios == []


def test_classify_identical_is_empty():
    r = classify(_expected(), _parsed(), **KW)
    assert (r.textual, r.structural, r.new_scenarios) == ([], [], [])


def test_classify_textual_only():
    r = classify(
        _expected(), _parsed(GOLDEN.replace("Quando paga", "Quando paga in contanti")), **KW
    )
    assert r.textual
    assert r.structural == []
    assert r.new_scenarios == []


def test_classify_missing_identity_tag_is_structural():
    r = classify(_expected(), _parsed(GOLDEN.replace("  @FDR3_001_01\n", "")), **KW)
    kinds = {c.kind for c in r.structural}
    assert "missing_tag" in kinds
    assert "removed_scenario" in kinds


def test_classify_duplicate_tag_is_structural():
    r = classify(_expected(), _parsed(GOLDEN.replace("@FDR3_001_02", "@FDR3_001_01")), **KW)
    assert any(c.kind == "duplicate_tag" and c.scenario_tag == "@FDR3_001_01" for c in r.structural)


def test_classify_wrong_prefix_is_structural():
    r = classify(_expected(), _parsed(GOLDEN.replace("@FDR3_001_02", "@FDR3_002_02")), **KW)
    assert any(c.kind == "wrong_tag" for c in r.structural)
    r = classify(_expected(), _parsed(GOLDEN.replace("@FDR3_001_02", "@ABC_001_02")), **KW)
    assert any(c.kind == "wrong_tag" for c in r.structural)


def test_classify_removed_scenario_is_structural():
    head = GOLDEN.split("  @negative")[0].rstrip("\n") + "\n"
    r = classify(_expected(), _parsed(head), **KW)
    assert [c.kind for c in r.structural] == ["removed_scenario"]
    assert r.structural[0].scenario_tag == "@FDR3_001_02"


def test_classify_new_valid_tag_is_new_scenario():
    extra = "\n  @positive\n  @FDR3_001_03\n  Scenario: Nuovo\n    Dato x\n    Allora y\n"
    r = classify(_expected(), _parsed(GOLDEN + extra), **KW)
    assert r.structural == []
    assert [s.identity_tag for s in r.new_scenarios] == ["@FDR3_001_03"]
    assert r.new_scenarios[0].polarity == TlPolarity.POSITIVO
    assert r.new_scenarios[0].kind == TlScenarioKind.SCENARIO


def test_classify_missing_header_is_structural():
    r = classify(_expected(), _parsed(GOLDEN.split("\n", 1)[1]), **KW)
    assert any(c.kind == "header_missing" for c in r.structural)
    r = classify(_expected(), _parsed(GOLDEN.replace("FEAT_001_FdR3", "FEAT_009_FdR3")), **KW)
    assert any(c.kind == "header_changed" for c in r.structural)


def test_classify_unknown_extra_tag_is_textual():
    r = classify(
        _expected(),
        _parsed(GOLDEN.replace("@runnable @positive", "@runnable @e2e @positive")),
        **KW,
    )
    assert r.textual
    assert r.structural == []


def _roundtrip_clean(mutate):
    exp = _expected()
    mutate(exp)
    r = classify(exp, parse_feature_file(render_feature_file(exp)), **KW)
    assert (r.textual, r.structural, r.new_scenarios) == ([], [], [])


def test_no_false_diff_docstring_indentation():
    def m(e):
        e.scenarios[0].steps = 'Dato un body\n  """\n  x\n    y\n  """\nQuando paga'

    _roundtrip_clean(m)


def test_no_false_diff_multiline_description():
    _roundtrip_clean(lambda e: setattr(e, "description", "a\n  b"))


def test_no_false_diff_crlf_and_inner_spaces():
    def m(e):
        e.scenarios[0].steps = "Dato  un   carrello\r\nQuando paga\r\nAllora vede la conferma"
        e.background_steps = "Dati  un utente\r\n  E altro"

    _roundtrip_clean(m)


def test_no_false_diff_int_example_cells():
    _roundtrip_clean(lambda e: e.scenarios[1].examples.update(rows=[[1, 2], [3, 4]]))


def test_rule_is_parse_error():
    p = parse_feature_file(
        "# qa-hub: FEAT_001_FdR3\n# language: it\nFunzionalità: x\n"
        "  Regola: r\n    Scenario: a\n      Dato b\n"
    )
    assert p.errors
    assert p.scenarios == []


def test_two_examples_blocks_is_parse_error():
    text = GOLDEN + "\n    Esempi:\n      | flowid | esito |\n      | z | y |\n"
    p = parse_feature_file(text)
    assert p.errors
    assert p.scenarios == []


def test_two_identity_tags_is_structural():
    r = classify(
        _expected(),
        _parsed(GOLDEN.replace("  @FDR3_001_01\n", "  @FDR3_001_01 @FDR3_001_09\n")),
        **KW,
    )
    assert any(c.kind == "duplicate_identity" for c in r.structural)


def test_detail_link_is_stripped():
    p = _parsed(GOLDEN.replace("#link:req-1|Requisito", "#link:req-1|Requisito  "))
    assert p.scenarios[0].detail_link == "req-1|Requisito"


# ── tag standard (design 2026-11-26) ─────────────────────────────────────────


def test_parse_reads_the_test_type_from_the_feature_tags():
    p = _parsed(GOLDEN.replace("@FDR3_001\n", "@FDR3_001 @api\n", 1))
    assert (p.feature_tag, p.test_type) == ("@FDR3_001", "api")
    assert _parsed().test_type is None


def test_parse_finds_the_identity_tag_wherever_the_test_type_is():
    p = _parsed(GOLDEN.replace("@FDR3_001\n", "@e2e @FDR3_001\n", 1))
    assert (p.feature_tag, p.test_type) == ("@FDR3_001", "e2e")


def test_parse_without_identity_tag_does_not_take_the_test_type_for_it():
    p = _parsed(GOLDEN.replace("@FDR3_001\n", "@integration\n", 1))
    assert p.feature_tag is None
    assert p.test_type == "integration"


def test_parse_reads_both_polarity_spellings_and_keeps_them_out_of_extra_tags():
    for pos, neg in (("positive", "negative"), ("positivo", "negativo")):
        text = GOLDEN.replace("@positive", f"@{pos}").replace("@negative", f"@{neg}")
        first, second = _parsed(text).scenarios
        assert (first.polarity, second.polarity) == (TlPolarity.POSITIVO, TlPolarity.NEGATIVO)
        assert first.extra_tags == ["runnable"] and second.extra_tags == []


def test_parse_keeps_every_other_tag_by_default():
    text = GOLDEN.replace("@runnable @positive", "@smoke @runnable @flaky @custom_x @positive")
    assert _parsed(text).scenarios[0].extra_tags == ["smoke", "runnable", "flaky", "custom_x"]


def test_parse_with_an_explicit_list_still_filters():
    text = GOLDEN.replace("@runnable @positive", "@smoke @runnable @positive")
    p = parse_feature_file(text, allowed_extra=("runnable",))
    assert p.scenarios[0].extra_tags == ["runnable"]


def test_classify_ignores_the_test_type_tag():
    with_type = GOLDEN.replace("@FDR3_001\n", "@FDR3_001 @integration\n", 1)
    r = classify(_expected(), _parsed(with_type), **KW)
    assert r.structural == [] and r.textual == []


def test_roundtrip_keeps_the_test_type_and_custom_tags():
    exp = _expected()
    exp.test_type = "e2e"
    exp.scenarios[0].extra_tags = ["smoke", "runnable", "flaky", "checkout"]
    p = parse_feature_file(render_feature_file(exp))
    assert p.test_type == "e2e"
    assert p.scenarios[0].extra_tags == ["smoke", "runnable", "flaky", "checkout"]
    assert classify(exp, p, **KW).textual == []


def test_parse_does_not_keep_identity_shaped_tags_as_extra_tags():
    # the API rejects these names on a scenario, so keeping them would block every later edit
    text = GOLDEN.replace("@runnable @positive", "@runnable @FDR3_001 @CHK_012 @smoke @positive")
    assert _parsed(text).scenarios[0].extra_tags == ["runnable", "smoke"]

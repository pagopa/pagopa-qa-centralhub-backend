# ruff: noqa: F811, S101
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.test_lifecycle import TlStepPhrase
from app.services.test_lifecycle.git_client import TlGitError
from app.services.test_lifecycle.step_index import (
    extract_step_patterns,
    extract_step_phrases,
    pattern_matches,
    rebuild_step_index,
)
from tests._db import TestSession
from tests.test_tl_features import _use_test_db  # noqa: F401  (fixture riusata)
from tests.tl_fakes import FakeGitClient


def test_extract_phrases_inherits_keyword_for_e_ma():
    text = (
        "# language: it\n"
        "Funzionalità: x\n"
        "  Contesto:\n"
        "    Dato un carrello\n"
        "    E un utente\n"
        "  Scenario: s\n"
        "    Quando pago\n"
        "    E confermo\n"
        "    Allora vedo l'esito\n"
        "    Ma non vedo errori\n"
        "  Scenario: t\n"
        "    Data una cosa\n"
    )
    assert extract_step_phrases(text) == [
        ("given", "un carrello"),
        ("given", "un utente"),
        ("when", "pago"),
        ("when", "confermo"),
        ("then", "vedo l'esito"),
        ("then", "non vedo errori"),
        ("given", "una cosa"),
    ]


def test_extract_phrases_english_and_rule():
    text = (
        "Feature: x\n"
        "  Rule: r\n"
        "    Scenario: s\n"
        "      Given a\n"
        "      When b\n"
        "      And c\n"
        "      Then d\n"
    )
    assert extract_step_phrases(text) == [
        ("given", "a"),
        ("when", "b"),
        ("when", "c"),
        ("then", "d"),
    ]


def test_extract_patterns_variants():
    src = """
from behave import given, when, then, step

@given(u"un utente {name}")
def a(ctx, name): pass

@when(r'pago {n:d} euro')
def b(ctx, n): pass

@then(pattern="esito ok")
def c(ctx): pass

@step("passo comune")
def d(ctx): pass

@given("non letterale " + x)
def e(ctx): pass
"""
    got = extract_step_patterns(src)
    assert ("given", "un utente {name}") in got
    assert ("when", "pago {n:d} euro") in got
    assert ("then", "esito ok") in got
    for kw in ("given", "when", "then"):
        assert (kw, "passo comune") in got
    assert not any("letterale" in p for _, p in got)
    assert extract_step_patterns("def broken(:\n") == []


def test_pattern_matches_parse_placeholders():
    assert pattern_matches("un utente {name}", "un utente Mario Rossi")
    assert pattern_matches("pago {n:d} euro", "pago 10 euro")
    assert pattern_matches('il campo "{f}" vale "{v}"', 'il campo "a" vale "b"')
    assert not pattern_matches("un utente {name}", "il cliente Mario")
    assert not pattern_matches("pago", "pago molto")  # match completo
    assert pattern_matches("costo (EUR) {x}", "costo (EUR) 5")  # metacaratteri letterali
    assert pattern_matches("graffe {{x}}", "graffe {x}")
    assert pattern_matches("Un utente {name}", "un UTENTE x")  # parse: case-insensitive
    assert not pattern_matches("Ok", "ok", regex=True)  # regex: case-sensitive
    assert pattern_matches("^ok$", "ok", regex=True)
    assert not pattern_matches("ok", "ok davvero", regex=True)


FEATURE_A = (
    "# language: it\nFunzionalità: a\n  Scenario: s\n"
    "    Dato un utente Mario\n    Quando pago 5 euro\n    Allora vedo l'esito\n"
)
FEATURE_B = (
    "# language: it\nFunzionalità: b\n  Scenario: s\n"
    "    Dato un utente Mario\n    Quando faccio altro\n"
)
STEPS = """
from behave import given, when
@given("un utente {name}")
def a(ctx, name): pass
@when("pago {n:d} euro")
def b(ctx, n): pass
"""


async def _rows():
    async with TestSession() as s:
        rows = (await s.execute(select(TlStepPhrase))).scalars().all()
        return {(r.keyword, r.text): (r.occurrences, r.implemented) for r in rows}


async def test_rebuild_marks_implemented():
    fake = FakeGitClient()
    fake.seed_file("src/api/a/features/a.feature", FEATURE_A)
    fake.seed_file("src/e2e/b/features/b.feature", FEATURE_B)
    fake.seed_file("src/integration/fdr3/feature/c.feature", "non gherkin {{{")
    fake.seed_file("src/api/a/steps/a_steps.py", STEPS)
    fake.seed_file("docs/other.feature", FEATURE_A)  # fuori da src: ignorato
    fake.seed_file("src/api/a/steps/big.py", "x")
    orig = fake.get_file

    async def get_file(path, ref):
        if path.endswith("big.py"):
            raise TlGitError(413, "file troppo grande")
        return await orig(path, ref)

    fake.get_file = get_file

    # una riga stantia da sostituire
    async with TestSession() as s:
        s.add(TlStepPhrase(keyword="then", text="vecchio", occurrences=9, implemented=True))
        await s.commit()

    async with TestSession() as s:
        n = await rebuild_step_index(s, fake, "main")
    assert n == 4
    assert await _rows() == {
        ("given", "un utente Mario"): (2, True),
        ("when", "pago 5 euro"): (1, True),
        ("then", "vedo l'esito"): (1, False),
        ("when", "faccio altro"): (1, False),
    }


async def test_rebuild_regex_module():
    fake = FakeGitClient()
    fake.seed_file("src/x/features/a.feature", "Feature: a\n Scenario: s\n  Given foo 12\n")
    fake.seed_file(
        "src/x/steps/s.py",
        "from behave import given, use_step_matcher\n"
        'use_step_matcher("re")\n'
        '@given(r"foo \\d+")\ndef a(c): pass\n',
    )
    async with TestSession() as s:
        await rebuild_step_index(s, fake, "main")
    assert (await _rows()) == {("given", "foo 12"): (1, True)}


@pytest.fixture
async def seeded():
    rows = [
        ("given", "un utente registrato", 2, False),
        ("given", "utente generico", 1, True),
        ("when", "il registrato paga", 9, True),
        ("given", "Utente anonimo", 5, False),
        ("then", "altro", 100, True),
    ]
    async with TestSession() as s:
        for kw, text, occ, impl in rows:
            s.add(TlStepPhrase(keyword=kw, text=text, occurrences=occ, implemented=impl))
        await s.commit()


async def test_steps_endpoint_ranking(client, seeded):
    r = await client.get("/api/v1/test-lifecycle/steps", params={"q": "utente"})
    assert r.status_code == 200
    assert [x["text"] for x in r.json()] == [
        "utente generico",  # prefisso, implementato
        "Utente anonimo",  # prefisso
        "un utente registrato",  # sottostringa
    ]
    assert set(r.json()[0]) == {"keyword", "text", "occurrences", "implemented"}

    r = await client.get("/api/v1/test-lifecycle/steps", params={"q": "REGISTRATO"})
    assert [x["text"] for x in r.json()] == ["il registrato paga", "un utente registrato"]

    r = await client.get("/api/v1/test-lifecycle/steps", params={"q": "utente", "keyword": "when"})
    assert r.json() == []

    r = await client.get("/api/v1/test-lifecycle/steps", params={"limit": 2})
    assert len(r.json()) == 2
    assert (
        await client.get("/api/v1/test-lifecycle/steps", params={"limit": 51})
    ).status_code == 422
    assert (await client.get("/api/v1/test-lifecycle/steps", params={"q": "%"})).json() == []
    assert (
        await client.get("/api/v1/test-lifecycle/steps", params={"keyword": "x"})
    ).status_code == 422

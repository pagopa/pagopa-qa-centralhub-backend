from __future__ import annotations

# ruff: noqa: S101
import pytest

from app.services.ea import release_impact as ri
from app.services.ea.graph_index import GraphIndex
from tests.ea_graph_fixture import kb


def _cs(*paths: str, repo: str = "pagopa-debt-position") -> ri.ChangeSet:
    return ri.ChangeSet(
        repo=repo,
        title="t",
        url="u",
        ref="PR #1",
        state="open",
        files=[ri.ChangedFile(p, "modified", 3, 1) for p in paths],
    )


class FakeChanges:
    def __init__(self, cs: ri.ChangeSet) -> None:
        self.cs = cs
        self.calls: list[tuple] = []

    async def pull_request(self, repo: str, number: int) -> ri.ChangeSet:
        self.calls.append(("pr", repo, number))
        return self.cs

    async def compare(self, repo: str, base: str, head: str) -> ri.ChangeSet:
        self.calls.append(("cmp", repo, base, head))
        return self.cs


def test_parse_request_accepts_pr_and_compare_links() -> None:
    assert ri.parse_request(
        "https://github.com/pagopa/pagopa-debt-position/pull/42"
    ) == ri.ImpactRequest(repo="pagopa-debt-position", number=42)
    assert ri.parse_request(
        "https://github.com/pagopa/pagopa-fdr/compare/v1.2.0...main"
    ) == ri.ImpactRequest(repo="pagopa-fdr", base="v1.2.0", head="main")
    assert ri.parse_request(repo="pagopa/pagopa-fdr", base="a", head="b").repo == "pagopa-fdr"
    with pytest.raises(ri.ImpactError, match="organizzazione"):
        ri.parse_request("https://github.com/other/repo/pull/1")
    with pytest.raises(ri.ImpactError):
        ri.parse_request("https://example.com")
    assert ri.parse_request("github.com/pagopa/pagopa-fdr/pull/7?w=1#diff").number == 7
    assert (
        ri.parse_request("https://github.com/pagopa/pagopa-fdr/compare/release/1.2...main").base
        == "release/1.2"
    )
    assert ri.parse_request("https://github.com/pagopa/pagopa-fdr/pull/7/files").number == 7
    for bad in (
        "https://github.com/pagopa/pagopa-fdr/compare/main",  # no "..."
        "https://evil.com/github.com/pagopa/x/pull/1",  # not GitHub
        "https://github.com/pagopa/pagopa-fdr/pull/abc",
        "github.com/-/-/compare/" + "!" * 5000,  # CodeQL ReDoS sample: refused at once
    ):
        with pytest.raises(ri.ImpactError):
            ri.parse_request(bad)
    with pytest.raises(ri.ImpactError):
        ri.parse_request()


def test_classify_and_change_risk() -> None:
    cats = ri.classify(
        [
            ri.ChangedFile(p, "m")
            for p in (
                "openapi/openapi.json",
                "helm/values-prod.yaml",
                "src/main/java/A.java",
                "src/test/java/ATest.java",
                "README.md",
                ".github/workflows/ci.yml",
            )
        ]
    )
    assert {k: v["files"] for k, v in cats.items()} == {
        "contract": 1,
        "config": 1,
        "code": 1,
        "test": 1,
        "docs": 1,
        "ci": 1,
    }
    assert ri.change_risk(cats) == "alto"
    assert ri.change_risk(ri.classify([ri.ChangedFile("src/a.py", "m")])) == "medio"
    assert (
        ri.change_risk(
            ri.classify([ri.ChangedFile("README.md", "m"), ri.ChangedFile("tests/x_test.py", "m")])
        )
        == "nessuno"
    )


def test_contract_change_rates_callers_and_data_and_lists_suites() -> None:
    res = ri.analyse(GraphIndex(kb()), _cs("openapi/openapi.json", "src/main/java/A.java"))
    assert res["component"]["id"] == "pagopa-debt-position"
    assert res["risk"] == "alto"
    by = {x["id"]: x for x in res["impacted"]}
    assert (
        by["pagopa-wisp-converter"]["risk"] == "alto"
        and by["pagopa-wisp-converter"]["relation"] == "chiamante"
    )
    assert by["pagopa-checkout-fe"]["risk"] == "medio" and by["pagopa-checkout-fe"]["depth"] == 2
    assert (
        by["dl:gpd-ingestion"]["risk"] == "alto"
        and by["dl:gpd-ingestion"]["relation"] == "dato a valle"
    )
    assert by["ds-gpd"]["relation"] == "dipendenza" and by["ds-gpd"]["risk"] == "basso"
    names = [s["name"] for s in res["suites"]]
    assert names[0] in ("integration/gpd", "Integration test di GPD Core")
    assert "integration/wisp" in names and "NRT wisp" in names
    gaps = {x["id"] for x in res["gaps"]}
    assert gaps == {"pagopa-gpd-payments", "pagopa-checkout-fe"}
    procs = {p["id"]: p for p in res["processes"]}
    assert procs["pr-gpd"]["services"] == ["Posizioni debitorie"]
    assert "pr-m1" in procs  # reached through WISP and Checkout
    assert res["highlight"][0] == "pagopa-debt-position"
    # GPD Payments both calls the changed component and is called by it: listed once
    ids = [x["id"] for x in res["impacted"]]
    assert len(ids) == len(set(ids)) and len(res["highlight"]) == len(set(res["highlight"]))
    assert by["pagopa-gpd-payments"]["risk"] == "alto"
    assert by["pagopa-gpd-payments"]["relations"] == ["chiamante", "dipendenza"]


def test_docs_only_change_has_no_impact() -> None:
    res = ri.analyse(GraphIndex(kb()), _cs("README.md"))
    assert res["risk"] == "nessuno" and res["impacted"] == []


def test_config_change_raises_dependency_risk() -> None:
    res = ri.analyse(GraphIndex(kb()), _cs("helm/values-prod.yaml"))
    by = {x["id"]: x for x in res["impacted"]}
    assert res["risk"] == "medio"
    assert by["ds-gpd"]["risk"] == "medio"
    assert by["pagopa-wisp-converter"]["risk"] == "medio"


async def test_release_impact_checks_the_repo_before_calling_github() -> None:
    g = GraphIndex(kb())
    fake = FakeChanges(_cs("src/a.py"))
    with pytest.raises(ri.ImpactError, match="non è nella mappa"):
        await ri.release_impact(g, fake, ri.ImpactRequest(repo="unknown", number=1))
    assert fake.calls == []
    res = await ri.release_impact(
        g, fake, ri.ImpactRequest(repo="pagopa-debt-position", base="a", head="b")
    )
    assert fake.calls == [("cmp", "pagopa-debt-position", "a", "b")]
    assert res["risk"] == "medio"


def test_rules_describe_every_category_and_match_change_risk() -> None:
    rules = ri.impact_rules()
    assert {c["key"] for c in rules["categories"]} == {k for k, *_ in ri.CATEGORIES}
    assert all(c["hint"] for c in rules["categories"])
    # the rating published for a category is the one the analysis gives to a change with only it
    sample = {
        "contract": "openapi/openapi.json",
        "data": "db/migration/V1.sql",
        "config": "helm/values-prod.yaml",
        "code": "src/a.py",
        "ci": ".github/workflows/ci.yml",
        "docs": "README.md",
        "test": "tests/x_test.py",
    }
    for c in rules["categories"]:
        got = ri.change_risk(ri.classify([ri.ChangedFile(sample[c["key"]], "m")]))
        assert got == c["risk"], c["key"]
    assert rules["promotion"] == {"from": "medio", "to": "alto", "min_direct_callers": 5}


def test_explain_says_which_files_decided_the_risk() -> None:
    res = ri.analyse(GraphIndex(kb()), _cs("openapi/openapi.json", "src/main/java/A.java"))
    ex = res["risk_explain"]
    assert ex["change_risk"] == "alto" and ex["promoted"] is False
    assert [t["key"] for t in ex["trigger"]] == ["contract"]
    assert ex["trigger"][0]["examples"] == ["openapi/openapi.json"]
    assert "Contratto API" in ex["summary"] and "alto" in ex["summary"]
    assert ri.analyse(GraphIndex(kb()), _cs("README.md"))["risk_explain"]["trigger"] == []


def test_many_direct_callers_promote_a_medium_change(monkeypatch: pytest.MonkeyPatch) -> None:
    g = GraphIndex(kb())
    direct = sum(1 for _ in g.callers("pagopa-debt-position", depth=1))
    assert direct >= 1
    monkeypatch.setattr(ri, "PROMOTE_MIN_DIRECT_CALLERS", direct)
    res = ri.analyse(g, _cs("src/a.py"))
    assert res["risk"] == "alto" and res["risk_explain"]["promoted"] is True
    assert f"{direct} chiamanti diretti" in res["risk_explain"]["summary"]
    monkeypatch.setattr(ri, "PROMOTE_MIN_DIRECT_CALLERS", direct + 1)
    res = ri.analyse(g, _cs("src/a.py"))
    assert res["risk"] == "medio" and res["risk_explain"]["promoted"] is False


def test_explanation_counts_callers_of_the_component_even_without_impact() -> None:
    g = GraphIndex(kb())
    graph_callers = sum(1 for _ in g.callers("pagopa-debt-position", depth=1))
    assert graph_callers >= 1
    res = ri.analyse(g, _cs("README.md"))
    assert res["impacted"] == [] and res["risk"] == "nessuno"
    assert res["risk_explain"]["direct_callers"] == graph_callers
    assert res["risk_explain"]["promoted"] is False

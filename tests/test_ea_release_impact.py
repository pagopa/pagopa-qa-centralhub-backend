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

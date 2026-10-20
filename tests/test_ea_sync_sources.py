from __future__ import annotations

# ruff: noqa: S101
import json

import httpx
import pytest

from app.services.ea.sync import derive
from app.services.ea.sync.extract import extract_repo, wanted_files
from app.services.ea.sync.sources import ConfluenceClient, GitHubClient, SourceError


def _gh(handler: httpx.MockTransport) -> GitHubClient:
    return GitHubClient("tok", client=httpx.AsyncClient(transport=handler))


async def test_github_lists_all_pages_of_repositories() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        if "page=2" in str(request.url):
            return httpx.Response(200, json=[{"name": "b", "archived": True, "pushed_at": None}])
        return httpx.Response(
            200,
            json=[{"name": "a", "default_branch": "develop", "pushed_at": "2026-10-01T10:00:00Z"}],
            headers={
                "Link": '<https://api.github.com/orgs/pagopa/repos?page=2>; rel="next"',
                "X-RateLimit-Remaining": "4999",
            },
        )

    gh = _gh(httpx.MockTransport(handler))
    repos = await gh.list_repos()
    assert [r.name for r in repos] == ["a", "b"]
    assert repos[0].default_branch == "develop" and repos[0].pushed_at is not None
    assert repos[1].archived and gh.rate_limit_remaining() == 4999


async def test_github_reads_files_with_one_graphql_query() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        seen.append(query)
        assert 'object(expression: "main:helm/values-prod.yaml")' in query
        return httpx.Response(
            200,
            json={"data": {"repository": {"f0": {"text": "a: 1"}, "f1": None}}},
        )

    gh = _gh(httpx.MockTransport(handler))
    files = await gh.get_files("svc", "main", ["helm/values-prod.yaml", "missing.md"])
    assert files == {"helm/values-prod.yaml": "a: 1"} and len(seen) == 1


async def test_github_reports_rate_limit_and_missing_access() -> None:
    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})

    with pytest.raises(SourceError, match="Limite"):
        await _gh(httpx.MockTransport(limited)).get_tree("svc", "main")

    def no_repo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"repository": None}, "errors": [{"message": "NOT_FOUND"}]}
        )

    with pytest.raises(SourceError, match="NOT_FOUND"):
        await _gh(httpx.MockTransport(no_repo)).get_files("svc", "main", ["a"])

    with pytest.raises(SourceError, match="GITHUB_TOKEN"):
        GitHubClient("")


async def test_confluence_follows_cursor_links() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/wiki/api/v2/spaces":
            assert request.url.params["keys"] == "QDP"
            return httpx.Response(200, json={"results": [{"id": "77"}]})
        if "cursor" in str(request.url):
            return httpx.Response(
                200, json={"results": [{"id": 2, "title": "B", "parentId": 1}], "_links": {}}
            )
        return httpx.Response(
            200,
            json={
                "results": [{"id": 1, "title": "A", "parentId": None}],
                "_links": {"next": "/wiki/api/v2/spaces/77/pages?cursor=xyz"},
            },
        )

    client = ConfluenceClient(
        "https://pagopa.atlassian.net",
        "a@b",
        "t",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    pages = await client.list_pages("QDP")
    assert pages == [{"id": "1", "t": "A", "par": ""}, {"id": "2", "t": "B", "par": "1"}]


def test_extract_and_derive_a_small_repository() -> None:
    tree = [
        "pom.xml",
        "helm/values-uat.yaml",
        "helm/values-prod.yaml",
        "src/test/java/ATest.java",
        "integration-test/src/features/a.feature",
        ".github/workflows/ci.yml",
        "openapi/openapi.json",
        "CODEOWNERS",
    ]
    assert "helm/values-uat.yaml" not in wanted_files(tree)
    files = {
        "pom.xml": "<artifactId>spring-boot-starter</artifactId>",
        "helm/values-prod.yaml": (
            "microservice-chart:\n  namespace: gps\n  envConfig:\n"
            '    A: "https://api.platform.pagopa.it/gpd/api/v1"\n'
            '    B: "https://x.pagopa-p-gps-cosmos.documents.azure.com"\n'
        ),
        ".github/workflows/ci.yml": (
            "name: CI\non: push\njobs:\n  it:\n"
            "    uses: pagopa/pagopa-platform-integration-test/x\n  sonar: {}\n"
        ),
        "openapi/openapi.json": '{"info": {"title": "My API"}}',
        "CODEOWNERS": "* @pagopa/team-gps",
    }
    facts = extract_repo("pagopa-gpd-new", tree, files)
    assert facts["lang"] == ["Java"] and facts["fw"] == ["Spring Boot"]
    assert facts["tas_ci"] and facts["tests"]["unit_files"] == 1 and facts["tests"]["features"] == 1
    assert derive.deps_for("pagopa-gpd-new", facts) == {
        "pagopa-debt-position": ("api", "https://api.platform.pagopa.it/gpd/api/v1"),
        "ds-gps": ("data", "https://x.pagopa-p-gps-cosmos.documents.azure.com"),
    }
    qa = derive.qa_observed("pagopa-gpd-new", facts)
    assert qa["unit_files"] == 1 and qa["sonar"] and qa["tas_ci_observed"]
    attrs = derive.component_attrs(facts)
    assert attrs["ns"] == "gps" and attrs["apis"] == [{"f": "openapi/openapi.json", "t": "My API"}]
    assert derive.guess_product("pagopa-gpd-new", facts, {"prod:gpd": "gps"}, {}) == "prod:gpd"
    assert (
        derive.guess_product("pagopa-gpd-x", {}, {}, {"pagopa-gpd-payments": "prod:gpd"})
        == "prod:gpd"
    )
    assert derive.guess_ctype("pagopa-mock-ec", facts) == "mock"
    assert derive.humanize("pagopa-gpd-new-thing") == "GPD New Thing"


def test_extract_recognises_scala_sbt_projects() -> None:
    tree = ["build.sbt", "src/main/scala/A.scala", "src/test/scala/ATest.scala"]
    facts = extract_repo(
        "nodo", tree, {"build.sbt": 'libraryDependencies += "com.typesafe.akka" %% "akka-actor"'}
    )
    assert (
        facts["lang"] == ["Scala"] and facts["fw"] == ["Akka"] and facts["tests"]["unit_files"] == 1
    )

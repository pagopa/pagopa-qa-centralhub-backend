from __future__ import annotations

# ruff: noqa: S101
from typing import Any

import httpx
import pytest

from app.services.status_page_changes import fetch_change_summary, summarize_commits
from app.services.status_page_github import EnrichGitHub, GitHubBudgetExhaustedError


def _commit(
    sha: str, message: str, author: str | None = "dev", html_url: str | None = None
) -> dict[str, Any]:
    return {
        "sha": sha,
        "html_url": html_url or f"https://github.com/pagopa/svc/commit/{sha}",
        "commit": {"message": message},
        "author": {"login": author} if author else None,
    }


def test_summarize_commits_rules_and_order() -> None:
    commits = [
        _commit("a1", "Merge remote-tracking branch 'origin/main'"),
        _commit("a2", "readme"),
        _commit("a3", "Bump to version 1.7.8 [skip ci]"),
        _commit("a4", "Merge pull request #12 from x/y\n\nAdd the thing\nmore"),
        _commit("a5", "Fix bug (#225)\n\nlong body", author=None),
        _commit("a6", "Merge branch 'dev'"),
    ]
    items, truncated = summarize_commits("svc", commits)
    assert truncated is False
    assert [i["sha"] for i in items] == ["a5", "a4", "a2"]
    assert items[0] == {
        "kind": "pr",
        "number": 225,
        "title": "Fix bug",
        "author": None,
        "url": "https://github.com/pagopa/svc/pull/225",
        "sha": "a5",
    }
    assert items[1]["kind"] == "pr"
    assert items[1]["number"] == 12
    assert items[1]["title"] == "Add the thing"
    assert items[1]["url"] == "https://github.com/pagopa/svc/pull/12"
    assert items[2] == {
        "kind": "commit",
        "number": None,
        "title": "readme",
        "author": "dev",
        "url": "https://github.com/pagopa/svc/commit/a2",
        "sha": "a2",
    }


def test_merge_pr_without_body_falls_back_to_subject() -> None:
    items, _ = summarize_commits("svc", [_commit("b1", "Merge pull request #7 from x/y")])
    assert items[0]["title"] == "Merge pull request #7 from x/y"
    assert items[0]["number"] == 7


def test_untrusted_html_url_is_replaced_by_sha_url() -> None:
    items, _ = summarize_commits("svc", [_commit("c1", "plain", html_url="https://evil.test/x")])
    assert items[0]["url"] == "https://github.com/pagopa/svc/commit/c1"


def test_cap_at_50_newest_first() -> None:
    commits = [_commit(f"s{i}", f"change {i}") for i in range(60)]
    items, truncated = summarize_commits("svc", commits)
    assert len(items) == 50
    assert truncated is True
    assert items[0]["sha"] == "s59"
    assert items[-1]["sha"] == "s10"


def _gh(handler: Any, calls: list[str]) -> httpx.AsyncClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


def _router(compares: dict[str, dict[str, Any]], tags: set[str] | None = None) -> Any:
    known = tags if tags is not None else {"1.0.0", "1.1.0"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/git/ref/tags/" in path:
            return httpx.Response(200 if path.rsplit("/", 1)[1] in known else 404, json={})
        key = path.rsplit("/compare/", 1)[1]
        return httpx.Response(200, json=compares[key])

    return handler


@pytest.mark.asyncio
async def test_forward() -> None:
    calls: list[str] = []
    cmp = {
        "status": "ahead",
        "total_commits": 2,
        "commits": [_commit("x1", "one (#1)"), _commit("x2", "two")],
    }
    async with _gh(_router({"1.0.0...1.1.0": cmp}), calls) as c:
        r = await fetch_change_summary(EnrichGitHub(c, 0), "svc", "1.0.0", "1.1.0")
    assert (r.status, r.direction, r.from_ref, r.to_ref) == ("OK", "forward", "1.0.0", "1.1.0")
    assert [i["sha"] for i in r.items] == ["x2", "x1"]
    assert r.total_commits == 2
    assert r.truncated is False
    assert r.error is None


@pytest.mark.asyncio
async def test_rollback_uses_reverse_compare() -> None:
    calls: list[str] = []
    compares = {
        "1.0.0...1.1.0": {"status": "behind", "total_commits": 0, "commits": []},
        "1.1.0...1.0.0": {
            "status": "ahead",
            "total_commits": 1,
            "commits": [_commit("r1", "gone")],
        },
    }
    async with _gh(_router(compares), calls) as c:
        r = await fetch_change_summary(EnrichGitHub(c, 0), "svc", "1.0.0", "1.1.0")
    assert r.status == "OK"
    assert r.direction == "rollback"
    assert [i["sha"] for i in r.items] == ["r1"]
    assert r.total_commits == 1
    assert sum("/compare/" in p for p in calls) == 2


@pytest.mark.asyncio
async def test_identical() -> None:
    cmp = {"status": "identical", "total_commits": 0, "commits": []}
    async with _gh(_router({"1.0.0...1.1.0": cmp}), []) as c:
        r = await fetch_change_summary(EnrichGitHub(c, 0), "svc", "1.0.0", "1.1.0")
    assert (r.status, r.direction, r.items, r.truncated) == ("OK", "identical", [], False)


@pytest.mark.asyncio
async def test_diverged_and_truncated_by_total() -> None:
    cmp = {"status": "diverged", "total_commits": 300, "commits": [_commit("d1", "x")]}
    async with _gh(_router({"1.0.0...1.1.0": cmp}), []) as c:
        r = await fetch_change_summary(EnrichGitHub(c, 0), "svc", "1.0.0", "1.1.0")
    assert r.direction == "diverged"
    assert r.total_commits == 300
    assert r.truncated is True


@pytest.mark.asyncio
async def test_unresolved_tag_is_unavailable_without_compare() -> None:
    calls: list[str] = []
    async with _gh(_router({}, tags={"1.0.0"}), calls) as c:
        r = await fetch_change_summary(EnrichGitHub(c, 0), "svc", "1.0.0", "9.9.9")
    assert r.status == "UNAVAILABLE"
    assert r.error == "versione non corrispondente a un tag"
    assert r.items == []
    assert not any("/compare/" in p for p in calls)


@pytest.mark.asyncio
async def test_unresolved_from_tag_skips_resolving_to() -> None:
    calls: list[str] = []
    async with _gh(_router({}, tags={"1.1.0"}), calls) as c:
        r = await fetch_change_summary(EnrichGitHub(c, 0), "svc", "9.9.9", "1.1.0")
    assert r.status == "UNAVAILABLE"
    assert len(calls) == 2  # only the two spellings of the from tag
    assert not any("1.1.0" in p for p in calls)


@pytest.mark.asyncio
async def test_budget_error_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})

    async with _gh(handler, []) as c:
        with pytest.raises(GitHubBudgetExhaustedError):
            await fetch_change_summary(EnrichGitHub(c, 0), "svc", "1.0.0", "1.1.0")

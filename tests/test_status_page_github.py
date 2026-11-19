from __future__ import annotations

# ruff: noqa: S101
import base64

import httpx
import pytest

from app.config import settings
from app.services.status_page_github import (
    EnrichGitHub,
    GitHubBudgetExhaustedError,
    GitHubNotFoundError,
    compare_path,
    contents_path,
    repo_path,
    resolve_tag,
)


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def _rl(remaining: int) -> dict[str, str]:
    return {"x-ratelimit-remaining": str(remaining)}


@pytest.mark.asyncio
async def test_get_json_sends_auth_and_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_token", "tok")
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json={"a": 1}, headers=_rl(4000))

    async with _client(httpx.MockTransport(handler)) as c:
        gh = EnrichGitHub(c, reserve=100)
        assert await gh.get_json("/repos/pagopa/x") == {"a": 1}
        assert gh.remaining == 4000
    assert str(seen[0].url) == "https://api.github.com/repos/pagopa/x"
    assert seen[0].headers["Authorization"] == "Bearer tok"
    assert seen[0].headers["Accept"] == "application/vnd.github+json"


@pytest.mark.asyncio
async def test_get_json_404() -> None:
    async with _client(httpx.MockTransport(lambda r: httpx.Response(404))) as c:
        with pytest.raises(GitHubNotFoundError):
            await EnrichGitHub(c, 100).get_json("/x")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429])
async def test_rate_limited_raises_budget(status: int) -> None:
    t = httpx.MockTransport(lambda r: httpx.Response(status, headers=_rl(0)))
    async with _client(t) as c:
        with pytest.raises(GitHubBudgetExhaustedError):
            await EnrichGitHub(c, 100).get_json("/x")


@pytest.mark.asyncio
async def test_403_with_remaining_is_http_error() -> None:
    t = httpx.MockTransport(lambda r: httpx.Response(403, headers=_rl(50)))
    async with _client(t) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await EnrichGitHub(c, 10).get_json("/x")


@pytest.mark.asyncio
async def test_server_error_is_http_error() -> None:
    async with _client(httpx.MockTransport(lambda r: httpx.Response(500))) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await EnrichGitHub(c, 100).get_json("/x")


@pytest.mark.asyncio
async def test_crossing_reserve_returns_once_then_raises() -> None:
    calls: list[int] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={}, headers=_rl(99))

    async with _client(httpx.MockTransport(handler)) as c:
        gh = EnrichGitHub(c, reserve=100)
        assert await gh.get_json("/x") == {}
        with pytest.raises(GitHubBudgetExhaustedError):
            await gh.get_json("/x")
    assert len(calls) == 1


def _tag_handler(existing: set[str], calls: list[str]):
    def handler(r: httpx.Request) -> httpx.Response:
        tag = r.url.path.split("/git/ref/tags/")[1]
        calls.append(tag)
        if tag in existing:
            return httpx.Response(200, json={"ref": f"refs/tags/{tag}"}, headers=_rl(4000))
        return httpx.Response(404, headers=_rl(4000))

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing", "version", "expected", "lookups"),
    [
        ({"1.2.3"}, "1.2.3", "1.2.3", ["1.2.3"]),
        ({"v1.2.3"}, "1.2.3", "v1.2.3", ["1.2.3", "v1.2.3"]),
        ({"1.2.3"}, "v1.2.3", "1.2.3", ["v1.2.3", "1.2.3"]),
        (set(), "1.2.3", None, ["1.2.3", "v1.2.3"]),
    ],
)
async def test_resolve_tag(
    existing: set[str], version: str, expected: str | None, lookups: list[str]
) -> None:
    calls: list[str] = []
    async with _client(httpx.MockTransport(_tag_handler(existing, calls))) as c:
        assert await resolve_tag(EnrichGitHub(c, 100), "repo", version) == expected
    assert calls == lookups


@pytest.mark.asyncio
async def test_resolve_tag_encodes_tag_and_repo() -> None:
    raw: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        raw.append(r.url.raw_path.decode())
        return httpx.Response(404)

    async with _client(httpx.MockTransport(handler)) as c:
        assert await resolve_tag(EnrichGitHub(c, 100), "repo", "a/b") is None
    assert raw[0] == "/repos/pagopa/repo/git/ref/tags/a%2Fb"


@pytest.mark.asyncio
async def test_resolve_tag_invalid_repository() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    async with _client(httpx.MockTransport(handler)) as c:
        assert await resolve_tag(EnrichGitHub(c, 100), "../x", "1.0.0") is None


@pytest.mark.asyncio
async def test_get_text_decodes_base64() -> None:
    body = {"content": base64.encodebytes(b"FROM python:3.12\n").decode(), "encoding": "base64"}
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json=body, headers=_rl(4000))

    async with _client(httpx.MockTransport(handler)) as c:
        text = await EnrichGitHub(c, 100).get_text("/repos/pagopa/x/contents/Dockerfile", "v1")
    assert text == "FROM python:3.12\n"
    assert seen[0].url.params["ref"] == "v1"


@pytest.mark.asyncio
async def test_get_text_404_none() -> None:
    async with _client(httpx.MockTransport(lambda r: httpx.Response(404))) as c:
        assert await EnrichGitHub(c, 100).get_text("/repos/pagopa/x/contents/f", "v1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["@evil.example/x", "/repos/../x", "/repos/x?a=b", "/repos//x", "/a#b", "/a b"]
)
async def test_unsafe_paths_rejected_without_request(path: str) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    async with _client(httpx.MockTransport(handler)) as c:
        with pytest.raises(ValueError):
            await EnrichGitHub(c, 100).get_json(path)


def test_builders_encode_and_validate() -> None:
    assert repo_path("repo", "git", "a/b") == "/repos/pagopa/repo/git/a%2Fb"
    assert contents_path("repo") == "/repos/pagopa/repo/contents"
    assert contents_path("repo", "src/a b.txt") == "/repos/pagopa/repo/contents/src/a%20b.txt"
    assert compare_path("repo", "v1/x", "v2") == "/repos/pagopa/repo/compare/v1%2Fx...v2"
    for bad in ("", ".", ".."):
        with pytest.raises(ValueError):
            repo_path("repo", bad)
        with pytest.raises(ValueError):
            compare_path("repo", bad, "v2")
    with pytest.raises(ValueError):
        repo_path("../x", "a")


@pytest.mark.asyncio
async def test_compare_path_is_accepted_by_get() -> None:
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r.url.raw_path.decode())
        return httpx.Response(200, json={}, headers=_rl(4000))

    async with _client(httpx.MockTransport(handler)) as c:
        await EnrichGitHub(c, 100).get_json(compare_path("repo", "v1", "v2"))
    assert seen == ["/repos/pagopa/repo/compare/v1...v2"]


@pytest.mark.asyncio
async def test_resolve_tag_empty_version_no_request() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    async with _client(httpx.MockTransport(handler)) as c:
        assert await resolve_tag(EnrichGitHub(c, 100), "repo", "") is None
        assert await resolve_tag(EnrichGitHub(c, 100), "repo", "..") is None


@pytest.mark.asyncio
async def test_get_text_malformed_base64_none() -> None:
    body = {"content": "abc", "encoding": "base64"}
    async with _client(httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as c:
        assert await EnrichGitHub(c, 100).get_text("/repos/pagopa/x/contents/f", "v1") is None


@pytest.mark.asyncio
async def test_no_authorization_header_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_token", "")
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json={})

    async with _client(httpx.MockTransport(handler)) as c:
        await EnrichGitHub(c, 100).get_json("/x")
    assert "Authorization" not in seen[0].headers

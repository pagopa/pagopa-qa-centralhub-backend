from __future__ import annotations

# ruff: noqa: S101
import httpx
import pytest

from app.config import settings
from app.services.status_page_sources import Reading, is_valid_repository, read_env, read_repo


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


@pytest.mark.asyncio
async def test_read_env_ok() -> None:
    async with _client(
        httpx.MockTransport(lambda r: httpx.Response(200, json={"version": "1.4.2"}))
    ) as c:
        assert await read_env(c, "DEV", "x") == Reading("DEV", "OK", "1.4.2", None)


@pytest.mark.asyncio
async def test_read_env_strips_release_prefix_keeps_v() -> None:
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"version": " Release v1.4.2 "}))
    async with _client(t) as c:
        assert (await read_env(c, "DEV", "x")).version == "v1.4.2"


@pytest.mark.asyncio
async def test_read_env_empty_body() -> None:
    async with _client(httpx.MockTransport(lambda r: httpx.Response(200, content=b""))) as c:
        assert (await read_env(c, "DEV", "x")).read_status == "EMPTY_BODY"


@pytest.mark.asyncio
async def test_read_env_no_version() -> None:
    async with _client(httpx.MockTransport(lambda r: httpx.Response(200, json={"name": "x"}))) as c:
        assert (await read_env(c, "DEV", "x")).read_status == "NO_VERSION"


@pytest.mark.asyncio
async def test_read_env_http_error_not_retried() -> None:
    calls: list[int] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    async with _client(httpx.MockTransport(handler)) as c:
        reading = await read_env(c, "DEV", "x")
    assert (reading.read_status, reading.error_detail) == ("ERROR", "HTTP 503")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_read_env_two_timeouts_is_error_with_two_attempts() -> None:
    calls: list[int] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectTimeout("boom")

    async with _client(httpx.MockTransport(handler)) as c:
        assert (await read_env(c, "DEV", "x")).read_status == "ERROR"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_read_env_retry_then_ok() -> None:
    calls: list[int] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("boom")
        return httpx.Response(200, json={"version": "1.0.0"})

    async with _client(httpx.MockTransport(handler)) as c:
        assert (await read_env(c, "DEV", "x")).read_status == "OK"


@pytest.mark.asyncio
async def test_read_env_url() -> None:
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(str(r.url))
        return httpx.Response(200, json={"version": "1"})

    async with _client(httpx.MockTransport(handler)) as c:
        await read_env(c, "UAT", "afmcalculator")
    assert seen == [
        "https://api.uat.platform.pagopa.it/shared/statuspage/v1/info?product=afmcalculator"
    ]


@pytest.mark.asyncio
async def test_read_repo_ok_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_token", "tok123")
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json={"tag_name": "Release 1.4.2"})

    async with _client(httpx.MockTransport(handler)) as c:
        reading = await read_repo(c, "pagopa-afm-calculator")
    assert reading == Reading("REPO", "OK", "1.4.2", None)
    assert seen[0].headers["Authorization"] == f"Bearer {settings.github_token}"
    assert str(seen[0].url) == (
        "https://api.github.com/repos/pagopa/pagopa-afm-calculator/releases/latest"
    )


@pytest.mark.parametrize("name", ["pagopa-afm-calculator", "a..b", "repo.name_1"])
def test_is_valid_repository_accepts_names(name: str) -> None:
    assert is_valid_repository(name)


@pytest.mark.parametrize("name", [".", "..", "...", "", "a/b", "a%2fb", "a b", "a?b", "a#b"])
def test_is_valid_repository_rejects_names(name: str) -> None:
    assert not is_valid_repository(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [".", "..", "../../orgs/pagopa"])
async def test_read_repo_rejects_unsafe_names_without_a_request(name: str) -> None:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json={"tag_name": "1.0.0"})

    async with _client(httpx.MockTransport(handler)) as c:
        reading = await read_repo(c, name)
    assert reading == Reading("REPO", "ERROR", None, "Invalid repository name")
    assert seen == []


@pytest.mark.asyncio
async def test_read_repo_no_release() -> None:
    async with _client(httpx.MockTransport(lambda r: httpx.Response(404))) as c:
        assert (await read_repo(c, "r")).read_status == "NO_RELEASE"


@pytest.mark.asyncio
async def test_read_repo_rate_limit() -> None:
    t = httpx.MockTransport(lambda r: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}))
    async with _client(t) as c:
        reading = await read_repo(c, "r")
    assert (reading.read_status, reading.error_detail) == ("ERROR", "GitHub rate limit")


@pytest.mark.asyncio
async def test_read_repo_other_status_is_error() -> None:
    async with _client(httpx.MockTransport(lambda r: httpx.Response(403))) as c:
        assert (await read_repo(c, "r")).error_detail == "HTTP 403"

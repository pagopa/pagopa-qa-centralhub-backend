"""Read-only clients for the sources of the EA sync: GitHub and Confluence.

Kept behind small protocols so tests (and the dry-run script) can plug fakes in.
GitHub calls per repository: one REST call for the tree, one GraphQL call for all the
metadata files (≈ 190 + 190 on a first run, far below the 5,000/hour limit).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

from app.config import settings
from app.services.ea.sync.rules import CONFLUENCE_SPACES, GITHUB_ORG

_GITHUB_API = "https://api.github.com"
_FILES_PER_QUERY = 40
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class SourceError(RuntimeError):
    """A source could not be read (network, auth, rate limit)."""


@dataclass(frozen=True)
class RepoInfo:
    name: str
    archived: bool
    fork: bool
    private: bool
    default_branch: str
    pushed_at: datetime | None
    description: str | None


@dataclass(frozen=True)
class RepoTree:
    sha: str
    paths: list[str]
    truncated: bool


class GitHubSource(Protocol):
    async def list_repos(self) -> list[RepoInfo]: ...

    async def get_tree(self, repo: str, branch: str) -> RepoTree: ...

    async def get_files(self, repo: str, branch: str, paths: list[str]) -> dict[str, str]: ...

    def rate_limit_remaining(self) -> int | None: ...


class ConfluenceSource(Protocol):
    async def list_pages(self, space_key: str) -> list[dict[str, str]]: ...


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class GitHubClient:
    def __init__(self, token: str, org: str = GITHUB_ORG, client: httpx.AsyncClient | None = None):
        if not token:
            raise SourceError("GITHUB_TOKEN non configurato: la sync non può leggere GitHub")
        self.org = org
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT)
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._remaining: int | None = None

    def rate_limit_remaining(self) -> int | None:
        return self._remaining

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = await self._client.request(method, url, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise SourceError(f"GitHub non raggiungibile: {exc}") from exc
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            self._remaining = int(remaining)
        if resp.status_code in (403, 429) and self._remaining == 0:
            raise SourceError("Limite di richieste GitHub esaurito, riprova più tardi")
        if resp.status_code == 401:
            raise SourceError("Token GitHub non valido")
        return resp

    async def list_repos(self) -> list[RepoInfo]:
        out: list[RepoInfo] = []
        url: str | None = f"{_GITHUB_API}/orgs/{self.org}/repos?type=all&per_page=100"
        while url:
            resp = await self._request("GET", url)
            if resp.status_code != 200:
                raise SourceError(f"Elenco repository GitHub: HTTP {resp.status_code}")
            for r in resp.json():
                out.append(
                    RepoInfo(
                        name=r["name"],
                        archived=bool(r.get("archived")),
                        fork=bool(r.get("fork")),
                        private=bool(r.get("private")),
                        default_branch=r.get("default_branch") or "main",
                        pushed_at=_parse_dt(r.get("pushed_at")),
                        description=r.get("description"),
                    )
                )
            url = resp.links.get("next", {}).get("url")
        return out

    async def get_tree(self, repo: str, branch: str) -> RepoTree:
        resp = await self._request(
            "GET",
            f"{_GITHUB_API}/repos/{self.org}/{repo}/git/trees/{branch}",
            params={"recursive": "1"},
        )
        if resp.status_code != 200:
            raise SourceError(f"Albero di {repo}: HTTP {resp.status_code}")
        data = resp.json()
        paths = [e["path"] for e in data.get("tree", []) if e.get("type") == "blob"]
        return RepoTree(sha=data["sha"], paths=paths, truncated=bool(data.get("truncated")))

    async def get_files(self, repo: str, branch: str, paths: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for start in range(0, len(paths), _FILES_PER_QUERY):
            chunk = paths[start : start + _FILES_PER_QUERY]
            blob_fragment = "{ ... on Blob { text } }"
            fields = "\n".join(
                f"f{i}: object(expression: {json.dumps(f'{branch}:{p}')}) {blob_fragment}"
                for i, p in enumerate(chunk)
            )
            query = (
                f"query {{ repository(owner: {json.dumps(self.org)}, name: {json.dumps(repo)}) "
                f"{{ {fields} }} }}"
            )
            resp = await self._request("POST", f"{_GITHUB_API}/graphql", json={"query": query})
            if resp.status_code != 200:
                raise SourceError(f"File di {repo}: HTTP {resp.status_code}")
            body = resp.json()
            repo_data = (body.get("data") or {}).get("repository")
            if repo_data is None:
                errors = "; ".join(e.get("message", "") for e in body.get("errors") or [])
                raise SourceError(f"File di {repo}: {errors or 'repository non accessibile'}")
            for i, p in enumerate(chunk):
                blob = repo_data.get(f"f{i}")
                if blob and blob.get("text") is not None:
                    out[p] = blob["text"]
        return out


class ConfluenceClient:
    """Confluence Cloud REST v2, with the Atlassian user already configured for Jira."""

    def __init__(
        self,
        base_url: str,
        email: str,
        token: str,
        client: httpx.AsyncClient | None = None,
    ):
        if not (base_url and email and token):
            raise SourceError(
                "Credenziali Atlassian non configurate (JIRA_BASE_URL/EMAIL/API_TOKEN)"
            )
        self.base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT, auth=(email, token))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            resp = await self._client.get(f"{self.base}{path}", params=params)
        except httpx.HTTPError as exc:
            raise SourceError(f"Confluence non raggiungibile: {exc}") from exc
        if resp.status_code != 200:
            raise SourceError(f"Confluence {path}: HTTP {resp.status_code}")
        return resp.json()  # type: ignore[no-any-return]

    async def list_pages(self, space_key: str) -> list[dict[str, str]]:
        spaces = await self._get("/wiki/api/v2/spaces", {"keys": space_key})
        results = spaces.get("results") or []
        if not results:
            raise SourceError(f"Spazio Confluence {space_key} non trovato o non accessibile")
        space_id = results[0]["id"]
        out: list[dict[str, str]] = []
        path: str | None = f"/wiki/api/v2/spaces/{space_id}/pages"
        params: dict[str, Any] | None = {"limit": 250, "status": "current"}
        while path:
            data = await self._get(path, params)
            for p in data.get("results") or []:
                out.append(
                    {
                        "id": str(p["id"]),
                        "t": p.get("title") or "",
                        "par": str(p.get("parentId") or ""),
                    }
                )
            path = (data.get("_links") or {}).get("next")
            params = None  # the next link already carries the cursor
        return out


async def list_all_pages(
    confluence: ConfluenceSource, spaces: tuple[str, ...] = CONFLUENCE_SPACES
) -> dict[str, list[dict[str, str]]]:
    results = await asyncio.gather(*(confluence.list_pages(s) for s in spaces))
    return dict(zip(spaces, results, strict=True))


def default_github() -> GitHubClient:
    return GitHubClient(settings.github_token)


def default_confluence() -> ConfluenceClient:
    return ConfluenceClient(settings.jira_base_url, settings.jira_email, settings.jira_api_token)

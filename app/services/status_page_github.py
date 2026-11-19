from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings
from app.services.status_page_sources import GITHUB_API_BASE, GITHUB_OWNER, is_valid_repository


class GitHubBudgetExhaustedError(Exception):
    """The GitHub rate-limit budget (minus the reserve) is used up."""


class GitHubNotFoundError(Exception):
    """GitHub answered 404 for the requested resource."""


class EnrichGitHub:
    """GitHub reader that stops before the rate limit eats into a reserve."""

    def __init__(self, client: httpx.AsyncClient, reserve: int) -> None:
        self._client = client
        self._reserve = reserve
        self._remaining: int | None = None

    @property
    def remaining(self) -> int | None:
        return self._remaining

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        return headers

    async def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        if self._remaining is not None and self._remaining < self._reserve:
            raise GitHubBudgetExhaustedError("GitHub rate-limit budget below the reserve")
        response = await self._client.get(
            f"{GITHUB_API_BASE}{path}", headers=self._headers(), params=params
        )
        raw = response.headers.get("x-ratelimit-remaining")
        if raw is not None and raw.isdigit():
            self._remaining = int(raw)
        if response.status_code in (403, 429) and self._remaining == 0:
            raise GitHubBudgetExhaustedError("GitHub rate limit exhausted")
        if response.status_code == 404:
            raise GitHubNotFoundError(path)
        response.raise_for_status()
        return response

    async def get_json(self, path: str) -> Any:
        return (await self._get(path)).json()

    async def get_text(self, path: str, ref: str) -> str | None:
        """Decoded file content from a contents API ``path`` at ``ref``; None on 404."""
        try:
            response = await self._get(path, params={"ref": ref})
        except GitHubNotFoundError:
            return None
        data = response.json()
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")


async def resolve_tag(gh: EnrichGitHub, repository: str, version: str) -> str | None:
    """The existing tag for ``version``, trying it as is and with the ``v`` toggled."""
    if not is_valid_repository(repository):
        return None
    alternate = version[1:] if version.startswith("v") else f"v{version}"
    segment = quote(repository, safe="")
    for tag in (version, alternate):
        if not tag:
            continue
        path = f"/repos/{GITHUB_OWNER}/{segment}/git/ref/tags/{quote(tag, safe='')}"
        try:
            await gh.get_json(path)
        except GitHubNotFoundError:
            continue
        return tag
    return None

from __future__ import annotations

import base64
import binascii
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings
from app.services.status_page_sources import GITHUB_API_BASE, GITHUB_OWNER, is_valid_repository

_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._~%/\-:]*$")


def _check_path(path: str) -> None:
    """Reject anything that could leave the intended GitHub API endpoint."""
    # The compare endpoint legitimately contains ``base...head``; drop that one separator
    # before looking for ``..`` traversal.
    probe = path.replace("...", "", 1) if "/compare/" in path else path
    if (
        not path.startswith("/")
        or "//" in path
        or ".." in probe
        or "?" in path
        or "#" in path
        or not _SAFE_PATH_RE.fullmatch(path)
    ):
        raise ValueError(f"Unsafe GitHub API path: {path!r}")


def _segment(value: str) -> str:
    if value in ("", ".", ".."):
        raise ValueError(f"Invalid GitHub API path segment: {value!r}")
    return quote(value, safe="")


def repo_path(repository: str, *segments: str) -> str:
    """``/repos/pagopa/{repository}/...`` with every segment validated and quoted."""
    if not is_valid_repository(repository):
        raise ValueError(f"Invalid repository name: {repository!r}")
    parts = [_segment(s) for s in segments]
    return "/".join(["/repos", GITHUB_OWNER, quote(repository, safe=""), *parts])


def contents_path(repository: str, file_path: str = "") -> str:
    """Contents API path; the repository root listing when ``file_path`` is empty."""
    segments = file_path.split("/") if file_path else []
    return repo_path(repository, "contents", *segments)


def compare_path(repository: str, base: str, head: str) -> str:
    """Compare API path for ``base...head``; refs are validated and quoted."""
    return f"{repo_path(repository, 'compare')}/{_segment(base)}...{_segment(head)}"


class GitHubBudgetExhaustedError(Exception):
    """The GitHub rate-limit budget (minus the reserve) is used up."""


class GitHubNotFoundError(Exception):
    """GitHub answered 404 for the requested resource."""


def _is_rate_limited(response: httpx.Response, remaining: int | None) -> bool:
    """A 429, or a 403 that GitHub marks as a primary or secondary rate limit."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if remaining == 0 or "retry-after" in response.headers:
        return True
    return "rate limit" in response.text.lower()


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
        _check_path(path)
        if self._remaining is not None and self._remaining < self._reserve:
            raise GitHubBudgetExhaustedError("GitHub rate-limit budget below the reserve")
        response = await self._client.get(
            f"{GITHUB_API_BASE}{path}", headers=self._headers(), params=params
        )
        raw = response.headers.get("x-ratelimit-remaining")
        if raw is not None and raw.isdigit():
            self._remaining = int(raw)
        if _is_rate_limited(response, self._remaining):
            raise GitHubBudgetExhaustedError("GitHub rate limit hit")
        if response.status_code == 404:
            raise GitHubNotFoundError(path)
        response.raise_for_status()
        return response

    async def get_json(self, path: str) -> Any:
        return (await self._get(path)).json()

    async def list_dir(self, path: str, ref: str) -> list[dict[str, Any]] | None:
        """Contents API directory listing at ``ref``; None on 404 or a non-directory."""
        try:
            data = (await self._get(path, params={"ref": ref})).json()
        except GitHubNotFoundError:
            return None
        if not isinstance(data, list):
            return None
        return [item for item in data if isinstance(item, dict)]

    async def get_text(self, path: str, ref: str) -> str | None:
        """Decoded file content from a contents API ``path`` at ``ref``; None on 404."""
        try:
            response = await self._get(path, params={"ref": ref})
        except GitHubNotFoundError:
            return None
        data = response.json()
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        try:
            return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            return None


async def resolve_tag(gh: EnrichGitHub, repository: str, version: str) -> str | None:
    """The existing tag for ``version``, trying it as is and with the ``v`` toggled."""
    if not version or not is_valid_repository(repository):
        return None
    alternate = version[1:] if version.startswith("v") else f"v{version}"
    for tag in (version, alternate):
        try:
            path = repo_path(repository, "git", "ref", "tags", tag)
            _check_path(path)
        except ValueError:
            continue
        try:
            await gh.get_json(path)
        except GitHubNotFoundError:
            continue
        return tag
    return None

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_OWNER = "pagopa"
MAX_ATTEMPTS = 2
_RELEASE_PREFIX = "Release "
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9._-]+")


def is_valid_repository(name: str) -> bool:
    """A GitHub repository name that is safe as a single URL path segment.

    Only letters, digits, ``.``, ``_`` and ``-``; names made only of dots (``.``, ``..``)
    are rejected because they would move the request to another GitHub API path.
    """
    return bool(_REPOSITORY_RE.fullmatch(name)) and name.strip(".") != ""


@dataclass(frozen=True)
class Reading:
    target: str
    read_status: str
    version: str | None
    error_detail: str | None


def clean_version(raw: str) -> str:
    """Trim and strip a leading ``Release ``; the leading ``v`` is kept as read."""
    value = raw.strip()
    if value.startswith(_RELEASE_PREFIX):
        value = value[len(_RELEASE_PREFIX) :].strip()
    return value


def _base_url(target: str) -> str:
    urls = {
        "DEV": settings.status_page_dev_base_url,
        "UAT": settings.status_page_uat_base_url,
        "PROD": settings.status_page_prod_base_url,
    }
    return urls[target].rstrip("/")


async def _get_with_retry(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """GET with one retry on timeout / transport errors; HTTP statuses are never retried."""
    last_exc: httpx.HTTPError | None = None
    for _ in range(MAX_ATTEMPTS):
        try:
            return await client.get(url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
    assert last_exc is not None  # noqa: S101
    raise last_exc


def _error(target: str, detail: str) -> Reading:
    return Reading(target, "ERROR", None, detail[:500])


async def read_env(client: httpx.AsyncClient, target: str, product_key: str) -> Reading:
    try:
        url = f"{_base_url(target)}{settings.status_page_info_path}"
        try:
            response = await _get_with_retry(client, url, params={"product": product_key})
        except httpx.HTTPError as exc:
            return _error(target, f"{type(exc).__name__}: {exc}"[:500])
        if response.status_code != 200:
            return _error(target, f"HTTP {response.status_code}")
        if not response.content.strip():
            return Reading(target, "EMPTY_BODY", None, None)
        try:
            body = response.json()
        except ValueError:
            return _error(target, "Invalid JSON body")
        version = body.get("version") if isinstance(body, dict) else None
        if not isinstance(version, str) or not clean_version(version):
            return Reading(target, "NO_VERSION", None, None)
        return Reading(target, "OK", clean_version(version), None)
    except Exception:
        logger.exception("read_env failed", target=target, product_key=product_key)
        raise


async def read_repo(client: httpx.AsyncClient, repository: str) -> Reading:
    try:
        if not is_valid_repository(repository):
            return _error("REPO", "Invalid repository name")
        segment = quote(repository, safe="")
        url = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{segment}/releases/latest"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        try:
            response = await _get_with_retry(client, url, headers=headers)
        except httpx.HTTPError as exc:
            return _error("REPO", f"{type(exc).__name__}: {exc}"[:500])
        if response.status_code in (403, 429) and (
            response.headers.get("x-ratelimit-remaining") == "0"
        ):
            return _error("REPO", "GitHub rate limit")
        if response.status_code == 404:
            return Reading("REPO", "NO_RELEASE", None, None)
        if response.status_code != 200:
            return _error("REPO", f"HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            return _error("REPO", "Invalid JSON body")
        tag = body.get("tag_name") if isinstance(body, dict) else None
        if not isinstance(tag, str) or not clean_version(tag):
            return Reading("REPO", "NO_VERSION", None, None)
        return Reading("REPO", "OK", clean_version(tag), None)
    except Exception:
        logger.exception("read_repo failed", repository=repository)
        raise

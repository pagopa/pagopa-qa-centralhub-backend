from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from app.services.status_page_github import (
    EnrichGitHub,
    compare_path,
    resolve_tag,
)
from app.services.status_page_sources import GITHUB_OWNER

MAX_ITEMS = 50
STATUS_OK = "OK"
STATUS_UNAVAILABLE = "UNAVAILABLE"
_GITHUB_WEB = "https://github.com/"
_SQUASH_RE = re.compile(r"\(#(\d+)\)$")
_MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+) from ")
_NOISE_PREFIXES = ("Merge branch ", "Merge remote-tracking branch ")


@dataclass
class ChangeResult:
    status: str
    direction: str | None = None
    from_ref: str | None = None
    to_ref: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    total_commits: int | None = None
    truncated: bool = False
    error: str | None = None


def _is_noise(subject: str) -> bool:
    return "[skip ci]" in subject or subject.startswith(_NOISE_PREFIXES)


def _pr_url(repository: str, number: int) -> str:
    return f"{_GITHUB_WEB}{GITHUB_OWNER}/{quote(repository, safe='')}/pull/{int(number)}"


def _commit_url(repository: str, commit: dict[str, Any], sha: str) -> str:
    html_url = commit.get("html_url")
    if isinstance(html_url, str) and html_url.startswith(_GITHUB_WEB):
        return html_url
    return f"{_GITHUB_WEB}{GITHUB_OWNER}/{quote(repository, safe='')}/commit/{quote(sha, safe='')}"


def _first_body_line(lines: list[str]) -> str | None:
    for line in lines[1:]:
        if line.strip():
            return line.strip()
    return None


def _to_item(repository: str, commit: dict[str, Any]) -> dict[str, Any] | None:
    message = str((commit.get("commit") or {}).get("message") or "")
    lines = message.splitlines()
    subject = lines[0].strip() if lines else ""
    if not subject or _is_noise(subject):
        return None
    sha = str(commit.get("sha") or "")
    author = (commit.get("author") or {}).get("login")
    author = author if isinstance(author, str) else None

    squash = _SQUASH_RE.search(subject)
    if squash:
        number = int(squash.group(1))
        title = subject[: squash.start()].rstrip() or subject
        return _pr_item(repository, number, title, author, sha)
    merge = _MERGE_PR_RE.match(subject)
    if merge:
        title = _first_body_line(lines) or subject
        return _pr_item(repository, int(merge.group(1)), title, author, sha)
    return {
        "kind": "commit",
        "number": None,
        "title": subject,
        "author": author,
        "url": _commit_url(repository, commit, sha),
        "sha": sha,
    }


def _pr_item(
    repository: str, number: int, title: str, author: str | None, sha: str
) -> dict[str, Any]:
    return {
        "kind": "pr",
        "number": number,
        "title": title,
        "author": author,
        "url": _pr_url(repository, number),
        "sha": sha,
    }


def summarize_commits(
    repository: str, commits: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], bool]:
    """Items (newest first, max 50) from compare commits (oldest first); and whether capped."""
    items = [item for c in reversed(commits) if (item := _to_item(repository, c)) is not None]
    return items[:MAX_ITEMS], len(items) > MAX_ITEMS


def _result_from_compare(
    direction: str, from_ref: str, to_ref: str, repository: str, data: dict[str, Any]
) -> ChangeResult:
    commits = data.get("commits") or []
    total = data.get("total_commits")
    total = total if isinstance(total, int) else len(commits)
    items, capped = summarize_commits(repository, commits)
    return ChangeResult(
        status=STATUS_OK,
        direction=direction,
        from_ref=from_ref,
        to_ref=to_ref,
        items=items,
        total_commits=total,
        truncated=capped or total > len(commits),
    )


async def fetch_change_summary(
    gh: EnrichGitHub, repository: str, from_version: str, to_version: str
) -> ChangeResult:
    """Summary of what changed between two deployed versions; budget errors propagate."""
    from_ref = await resolve_tag(gh, repository, from_version)
    if from_ref is None:
        return ChangeResult(
            status=STATUS_UNAVAILABLE,
            error="versione non corrispondente a un tag",
        )
    to_ref = await resolve_tag(gh, repository, to_version)
    if to_ref is None:
        return ChangeResult(
            status=STATUS_UNAVAILABLE,
            from_ref=from_ref,
            to_ref=to_ref,
            error="versione non corrispondente a un tag",
        )
    data = await gh.get_json(compare_path(repository, from_ref, to_ref))
    status = data.get("status") if isinstance(data, dict) else None
    if status == "ahead":
        return _result_from_compare("forward", from_ref, to_ref, repository, data)
    if status == "diverged":
        return _result_from_compare("diverged", from_ref, to_ref, repository, data)
    if status == "identical":
        return ChangeResult(
            status=STATUS_OK,
            direction="identical",
            from_ref=from_ref,
            to_ref=to_ref,
            total_commits=0,
        )
    if status == "behind":
        back = await gh.get_json(compare_path(repository, to_ref, from_ref))
        if isinstance(back, dict) and back.get("status") in ("ahead", "diverged"):
            return _result_from_compare("rollback", from_ref, to_ref, repository, back)
    return ChangeResult(
        status=STATUS_UNAVAILABLE,
        from_ref=from_ref,
        to_ref=to_ref,
        error="confronto non determinabile",
    )

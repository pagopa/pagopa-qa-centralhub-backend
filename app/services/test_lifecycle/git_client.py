"""GitHub write client for the Test Lifecycle git integration."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

from app.config import settings

_PAGE_SIZE = 100
_SHA_RE = re.compile(r"[0-9a-f]{40}")
# Branch e percorsi possono arrivare da link incollati dagli utenti: solo caratteri che non
# cambiano l'API chiamata (niente `..`, `?`, `#`, `\`, caratteri di controllo).
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+@/-]{0,254}")
_PATH_SEGMENT_RE = re.compile(r"[^\x00-\x1f\x7f/?#\\]{1,255}")

def _ref(ref: str) -> str:
    """Branch o sha validato e codificato per l'URL; `TlGitError(400)` se non è sicuro."""
    if not _REF_RE.fullmatch(ref) or ".." in ref or "//" in ref or ref.endswith(("/", ".")):
        raise TlGitError(400, "nome di branch non valido")
    return quote(ref, safe="/")


def _path(path: str) -> str:
    """Percorso di file validato (segmenti non vuoti, niente `.`/`..`) e codificato."""
    segments = path.split("/")
    if len(path) > 1024 or not all(
        _PATH_SEGMENT_RE.fullmatch(seg) and seg not in (".", "..") for seg in segments
    ):
        raise TlGitError(400, "percorso del file non valido")
    return quote(path, safe="/")


PullState = Literal["open", "merged", "closed"]
FileStatus = Literal["added", "modified", "removed", "renamed"]


class TlGitError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub {status}: {message}")
        self.status = status
        self.message = message


class TlGitNotConfigured(Exception):  # noqa: N818 - name fixed by the interface
    pass


@dataclass(frozen=True)
class GitFile:
    path: str
    text: str
    blob_sha: str


@dataclass(frozen=True)
class PullInfo:
    number: int
    url: str
    state: PullState
    merge_commit_sha: str | None
    head_branch: str


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: FileStatus
    previous_path: str | None = None


def _pull_from(data: dict[str, Any]) -> PullInfo:
    if data.get("merged") or data.get("merged_at"):
        state: PullState = "merged"
    elif data.get("state") == "closed":
        state = "closed"
    else:
        state = "open"
    return PullInfo(
        number=data["number"],
        url=data.get("html_url", ""),
        state=state,
        merge_commit_sha=data.get("merge_commit_sha") if state == "merged" else None,
        head_branch=(data.get("head") or {}).get("ref", ""),
    )


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError as exc:
        raise TlGitError(502, "risposta GitHub non valida (JSON atteso)") from exc


def _error_message(resp: httpx.Response) -> str:
    try:
        msg = _json(resp).get("message")
        if msg:
            return str(msg)
    except (ValueError, AttributeError):
        pass  # noqa: S110 - non-JSON body, fall back to raw text
    return resp.text or resp.reason_phrase or "error"


class TlGitClient:
    BASE_URL = "https://api.github.com"

    def __init__(
        self, token: str, repo: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.token = token
        self.repo = repo
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        ok_statuses: tuple[int, ...] = (),
    ) -> httpx.Response:
        url = f"{self.BASE_URL}/repos/{self.repo}/{path}"
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=30.0) as client:
                resp = await client.request(
                    method, url, params=params, json=json, headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise TlGitError(502, f"GitHub non raggiungibile: {exc}") from exc
        if resp.status_code >= 400 and resp.status_code not in ok_statuses:
            raise TlGitError(resp.status_code, _error_message(resp))
        return resp

    async def head_sha(self, branch: str) -> str:
        resp = await self._request("GET", f"git/ref/heads/{_ref(branch)}")
        try:
            return _json(resp)["object"]["sha"]
        except (KeyError, TypeError) as exc:
            raise TlGitError(502, f"risposta GitHub non valida: {exc}") from exc

    async def branch_exists(self, branch: str) -> bool:
        resp = await self._request(
            "GET", f"git/ref/heads/{_ref(branch)}", ok_statuses=(404,)
        )
        return resp.status_code != 404

    async def create_branch(self, branch: str, from_sha: str) -> None:
        resp = await self._request(
            "POST",
            "git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": from_sha},
            ok_statuses=(422,),
        )
        if resp.status_code == 422 and "already exists" not in _error_message(resp):
            raise TlGitError(422, _error_message(resp))

    async def reset_branch(self, branch: str, sha: str) -> None:
        """Riporta il branch su `sha` (force): usato per riallineare il branch di una PR a main."""
        await self._request(
            "PATCH",
            f"git/refs/heads/{_ref(branch)}",
            json={"sha": sha, "force": True},
        )

    async def get_file(self, path: str, ref: str) -> GitFile | None:
        resp = await self._request(
            "GET", f"contents/{_path(path)}", params={"ref": ref}, ok_statuses=(404,)
        )
        if resp.status_code == 404:
            return None
        data = _json(resp)
        if isinstance(data, list):
            raise TlGitError(400, "il percorso è una cartella")
        if data.get("encoding") != "base64" or "content" not in data:
            raise TlGitError(413, "file troppo grande")
        try:
            raw = base64.b64decode(data["content"].replace("\n", ""))
            return GitFile(path=path, text=raw.decode("utf-8-sig"), blob_sha=data["sha"])
        except (ValueError, KeyError) as exc:
            raise TlGitError(502, f"contenuto del file non valido: {exc}") from exc

    async def put_file(
        self, path: str, text: str, branch: str, message: str, blob_sha: str | None = None
    ) -> str:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if blob_sha:
            body["sha"] = blob_sha
        resp = await self._request("PUT", f"contents/{_path(path)}", json=body)
        return _json(resp)["commit"]["sha"]

    async def delete_file(self, path: str, branch: str, message: str, blob_sha: str) -> str:
        resp = await self._request(
            "DELETE",
            f"contents/{_path(path)}",
            json={"message": message, "sha": blob_sha, "branch": branch},
        )
        return _json(resp)["commit"]["sha"]

    async def find_open_pull(self, head_branch: str) -> PullInfo | None:
        owner = self.repo.split("/", 1)[0]
        resp = await self._request(
            "GET", "pulls", params={"head": f"{owner}:{head_branch}", "state": "open"}
        )
        pulls = _json(resp)
        return _pull_from(pulls[0]) if pulls else None

    async def create_pull(self, head: str, base: str, title: str, body: str) -> PullInfo:
        resp = await self._request(
            "POST", "pulls", json={"head": head, "base": base, "title": title, "body": body}
        )
        return _pull_from(_json(resp))

    async def get_pull(self, number: int) -> PullInfo:
        resp = await self._request("GET", f"pulls/{number}")
        return _pull_from(_json(resp))

    async def close_pull(self, number: int) -> None:
        """Closes a pull request without merging it (it can be reopened on GitHub)."""
        await self._request("PATCH", f"pulls/{number}", json={"state": "closed"})

    async def compare(self, base_sha: str, head_sha: str) -> list[ChangedFile]:
        out: list[ChangedFile] = []
        page = 1
        while True:
            resp = await self._request(
                "GET",
                f"compare/{_ref(base_sha)}...{_ref(head_sha)}",
                params={"per_page": str(_PAGE_SIZE), "page": str(page)},
            )
            files = _json(resp).get("files", [])
            for f in files:
                status = f.get("status")
                if status not in ("added", "removed", "renamed"):
                    status = "modified"
                out.append(
                    ChangedFile(
                        path=f["filename"],
                        status=status,
                        previous_path=f.get("previous_filename") if status == "renamed" else None,
                    )
                )
            if len(files) < _PAGE_SIZE:
                return out
            page += 1

    async def list_tree(self, ref: str) -> list[str]:
        """Blob paths of the tree at `ref` (a commit sha, or a branch name resolved via head_sha).

        Raises TlGitError if GitHub truncated the listing, so callers never act on partial data.
        """
        sha = _ref(ref) if _SHA_RE.fullmatch(ref) else await self.head_sha(ref)
        resp = await self._request("GET", f"git/trees/{sha}", params={"recursive": "1"})
        data = _json(resp)
        if data.get("truncated"):
            raise TlGitError(502, "elenco dei file troncato da GitHub (repository troppo grande)")
        return [e["path"] for e in data.get("tree", []) if e.get("type") == "blob"]


def get_git_client() -> TlGitClient:
    if settings.tl_git_mode == "local":
        if not settings.tl_local_repo_path:
            raise TlGitNotConfigured("Modalità Git locale senza tl_local_repo_path")
        from app.services.test_lifecycle.local_git import LocalGitClient

        return LocalGitClient(settings.tl_local_repo_path)  # type: ignore[return-value]
    token = settings.tl_github_token or settings.tas_github_token
    if not token:
        raise TlGitNotConfigured("Nessun token GitHub configurato")
    return TlGitClient(token, settings.tl_repo)

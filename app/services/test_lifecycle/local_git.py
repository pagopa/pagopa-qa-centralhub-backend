"""Modalità Git locale (solo sviluppo): il "GitHub" dell'hub è un clone sul disco.

Stessa interfaccia di `TlGitClient`, ma ogni operazione avviene nel clone indicato da
`tl_local_repo_path` con i comandi git; nulla viene mai inviato a un remoto.

- branch e commit sono scritti con i comandi "plumbing" (indice temporaneo, `commit-tree`,
  `update-ref`): la copia di lavoro del clone e il branch su cui si trova non vengono toccati;
- le PR sono simulate in `<git-dir>/qa-hub-prs.json`;
- il merge di una PR e la pulizia finale si fanno con `scripts/tl_local_git.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.services.test_lifecycle.git_client import (
    ChangedFile,
    FileStatus,
    GitFile,
    PullInfo,
    TlGitError,
)

_AUTHOR = {
    "GIT_AUTHOR_NAME": "QA Hub (locale)",
    "GIT_AUTHOR_EMAIL": "qa-hub@localhost",
    "GIT_COMMITTER_NAME": "QA Hub (locale)",
    "GIT_COMMITTER_EMAIL": "qa-hub@localhost",
}
_HUB_PREFIX = "qa-hub/"
_STATUS: dict[str, FileStatus] = {"A": "added", "M": "modified", "D": "removed", "R": "renamed"}


def _run(repo: str, *args: str, env: dict[str, str] | None = None, stdin: str | None = None) -> str:
    """Esegue git nel clone; errore -> TlGitError(500) con lo stderr."""
    proc = subprocess.run(  # noqa: S603 - argomenti costruiti dal codice, nessuna shell
        ["git", "-C", repo, *args],  # noqa: S607
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, **_AUTHOR, **(env or {})},
    )
    if proc.returncode != 0:
        raise TlGitError(500, f"git {' '.join(args[:2])}: {proc.stderr.strip()}")
    return proc.stdout


def _ok(repo: str, *args: str) -> bool:
    return (
        subprocess.run(  # noqa: S603
            ["git", "-C", repo, *args],  # noqa: S607
            capture_output=True,
        ).returncode
        == 0
    )


def _resolve(repo: str, ref: str) -> str | None:
    """Sha del commit per un branch locale o uno sha; None se non esiste."""
    for candidate in (f"refs/heads/{ref}", ref):
        if _ok(repo, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"):
            return _run(repo, "rev-parse", f"{candidate}^{{commit}}").strip()
    return None


def _pulls_file(repo: str) -> Path:
    git_dir = Path(_run(repo, "rev-parse", "--absolute-git-dir").strip())
    return git_dir / "qa-hub-prs.json"


def _load_pulls(repo: str) -> list[dict[str, Any]]:
    path = _pulls_file(repo)
    return json.loads(path.read_text()) if path.exists() else []


def _save_pulls(repo: str, pulls: list[dict[str, Any]]) -> None:
    _pulls_file(repo).write_text(json.dumps(pulls, indent=2))


def _pull_info(data: dict[str, Any]) -> PullInfo:
    return PullInfo(
        number=data["number"],
        url=f"local://pull/{data['number']}",
        state=data["state"],
        merge_commit_sha=data.get("merge_commit_sha"),
        head_branch=data["head"],
    )


def _commit_tree_change(repo: str, branch: str, parent: str, message: str, edit: list[str]) -> str:
    """Nuovo commit sopra `parent` applicando `git update-index <edit>` a un indice temporaneo."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        _run(repo, "read-tree", parent, env=env)
        _run(repo, "update-index", *edit, env=env)
        tree = _run(repo, "write-tree", env=env).strip()
    commit = _run(repo, "commit-tree", tree, "-p", parent, "-m", message).strip()
    _run(repo, "update-ref", f"refs/heads/{branch}", commit, parent)
    return commit


class LocalGitClient:
    """Implementazione locale dell'interfaccia di `TlGitClient` (vedi modulo)."""

    def __init__(self, repo_path: str) -> None:
        self.repo = repo_path

    async def _call(self, fn: Any, *args: Any) -> Any:
        return await asyncio.to_thread(fn, *args)

    # ── branch ────────────────────────────────────────────────────────────────

    async def head_sha(self, branch: str) -> str:
        sha = await self._call(_resolve, self.repo, branch)
        if sha is None or not await self.branch_exists(branch):
            raise TlGitError(404, f"branch {branch} inesistente")
        return sha

    async def branch_exists(self, branch: str) -> bool:
        return await self._call(
            _ok, self.repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
        )

    async def create_branch(self, branch: str, from_sha: str) -> None:
        if await self.branch_exists(branch):
            return  # idempotente, come il client GitHub su "already exists"
        await self._call(_run, self.repo, "update-ref", f"refs/heads/{branch}", from_sha)

    async def reset_branch(self, branch: str, sha: str) -> None:
        if not await self.branch_exists(branch):
            raise TlGitError(422, "Reference does not exist")
        await self._call(_run, self.repo, "update-ref", f"refs/heads/{branch}", sha)

    # ── file ──────────────────────────────────────────────────────────────────

    def _get_file(self, path: str, ref: str) -> GitFile | None:
        sha = _resolve(self.repo, ref)
        if sha is None:
            return None
        spec = f"{sha}:{path}"
        if not _ok(self.repo, "cat-file", "-e", spec):
            return None
        if _run(self.repo, "cat-file", "-t", spec).strip() != "blob":
            raise TlGitError(400, "il percorso è una cartella")
        text = _run(self.repo, "cat-file", "blob", spec)
        blob = _run(self.repo, "rev-parse", spec).strip()
        return GitFile(path=path, text=text.lstrip("﻿"), blob_sha=blob)

    async def get_file(self, path: str, ref: str) -> GitFile | None:
        return await self._call(self._get_file, path, ref)

    def _put_file(
        self, path: str, text: str, branch: str, message: str, blob_sha: str | None
    ) -> str:
        parent = _resolve(self.repo, branch)
        if parent is None or not _ok(
            self.repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
        ):
            raise TlGitError(404, f"branch {branch} inesistente")
        current = self._get_file(path, branch)
        if current is not None and current.blob_sha != blob_sha:
            raise TlGitError(409, f"{path} non corrisponde allo sha indicato")
        if current is None and blob_sha is not None:
            raise TlGitError(409, f"{path} non esiste")
        blob = _run(self.repo, "hash-object", "-w", "--stdin", stdin=text).strip()
        return _commit_tree_change(
            self.repo, branch, parent, message, ["--add", "--cacheinfo", f"100644,{blob},{path}"]
        )

    async def put_file(
        self, path: str, text: str, branch: str, message: str, blob_sha: str | None = None
    ) -> str:
        return await self._call(self._put_file, path, text, branch, message, blob_sha)

    def _delete_file(self, path: str, branch: str, message: str, blob_sha: str) -> str:
        current = self._get_file(path, branch)
        if current is None:
            raise TlGitError(404, f"{path} non esiste su {branch}")
        if current.blob_sha != blob_sha:
            raise TlGitError(409, f"{path} does not match {blob_sha}")
        parent = _resolve(self.repo, branch)
        assert parent is not None  # noqa: S101 - il file esiste, quindi il branch pure
        return _commit_tree_change(self.repo, branch, parent, message, ["--force-remove", path])

    async def delete_file(self, path: str, branch: str, message: str, blob_sha: str) -> str:
        return await self._call(self._delete_file, path, branch, message, blob_sha)

    # ── pull request simulate ─────────────────────────────────────────────────

    async def find_open_pull(self, head_branch: str) -> PullInfo | None:
        for p in await self._call(_load_pulls, self.repo):
            if p["head"] == head_branch and p["state"] == "open":
                return _pull_info(p)
        return None

    def _create_pull(self, head: str, base: str, title: str, body: str) -> PullInfo:
        pulls = _load_pulls(self.repo)
        data = {
            "number": max((p["number"] for p in pulls), default=0) + 1,
            "head": head,
            "base": base,
            "title": title,
            "body": body,
            "state": "open",
            "merge_commit_sha": None,
        }
        pulls.append(data)
        _save_pulls(self.repo, pulls)
        return _pull_info(data)

    async def create_pull(self, head: str, base: str, title: str, body: str) -> PullInfo:
        return await self._call(self._create_pull, head, base, title, body)

    async def get_pull(self, number: int) -> PullInfo:
        for p in await self._call(_load_pulls, self.repo):
            if p["number"] == number:
                return _pull_info(p)
        raise TlGitError(404, f"PR {number} inesistente")

    # ── confronto e albero ────────────────────────────────────────────────────

    def _compare(self, base_sha: str, head_sha: str) -> list[ChangedFile]:
        for sha in (base_sha, head_sha):
            if _resolve(self.repo, sha) is None:
                raise TlGitError(404, f"commit {sha} inesistente")
        out: list[ChangedFile] = []
        for line in _run(self.repo, "diff", "--name-status", "-M", base_sha, head_sha).splitlines():
            parts = line.split("\t")
            code = parts[0][0]
            if code == "R":
                out.append(ChangedFile(path=parts[2], status="renamed", previous_path=parts[1]))
            else:
                out.append(ChangedFile(path=parts[1], status=_STATUS.get(code, "modified")))
        return out

    async def compare(self, base_sha: str, head_sha: str) -> list[ChangedFile]:
        return await self._call(self._compare, base_sha, head_sha)

    async def list_tree(self, ref: str) -> list[str]:
        sha = await self._call(_resolve, self.repo, ref)
        if sha is None:
            raise TlGitError(404, f"ref {ref} inesistente")
        out = await self._call(_run, self.repo, "ls-tree", "-r", "--name-only", sha)
        return out.splitlines()


# ── comandi di sviluppo (scripts/tl_local_git.py) ─────────────────────────────


def local_merge(repo: str, number: int) -> str:
    """Merge della PR simulata nel suo branch base (commit di merge, nessun checkout)."""
    pulls = _load_pulls(repo)
    pull = next((p for p in pulls if p["number"] == number), None)
    if pull is None or pull["state"] != "open":
        raise TlGitError(409, f"PR {number} non aperta")
    base = _resolve(repo, pull["base"])
    head = _resolve(repo, pull["head"])
    if base is None or head is None:
        raise TlGitError(404, "branch della PR inesistente")
    proc = subprocess.run(  # noqa: S603
        ["git", "-C", repo, "merge-tree", "--write-tree", base, head],  # noqa: S607
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise TlGitError(409, f"conflitti di merge:\n{proc.stdout.strip()}")
    tree = proc.stdout.splitlines()[0].strip()
    message = f"Merge pull request #{number} from {pull['head']}\n\n{pull['title']}"
    commit = _run(repo, "commit-tree", tree, "-p", base, "-p", head, "-m", message).strip()
    _run(repo, "update-ref", f"refs/heads/{pull['base']}", commit, base)
    pull["state"] = "merged"
    pull["merge_commit_sha"] = commit
    _save_pulls(repo, pulls)
    return commit


def local_close(repo: str, number: int) -> None:
    pulls = _load_pulls(repo)
    for p in pulls:
        if p["number"] == number and p["state"] == "open":
            p["state"] = "closed"
            _save_pulls(repo, pulls)
            return
    raise TlGitError(409, f"PR {number} non aperta")


def local_write(repo: str, branch: str, path: str, text: str, message: str) -> str:
    """Commit di un file su un branch (es. `main`) come se l'avesse fatto qualcun altro."""
    client = LocalGitClient(repo)
    current = client._get_file(path, branch)
    return client._put_file(path, text, branch, message, current.blob_sha if current else None)


def local_remove(repo: str, branch: str, path: str, message: str) -> str:
    parent = _resolve(repo, branch)
    if parent is None:
        raise TlGitError(404, f"branch {branch} inesistente")
    return _commit_tree_change(repo, branch, parent, message, ["--force-remove", path])


def local_reset(repo: str, base: str = "main") -> None:
    """Cancella i branch `qa-hub/*` e le PR simulate, riporta `base` su `origin/<base>`."""
    refs = _run(repo, "for-each-ref", "--format=%(refname)", f"refs/heads/{_HUB_PREFIX}").split()
    for ref in refs:
        _run(repo, "update-ref", "-d", ref)
    origin = _resolve(repo, f"refs/remotes/origin/{base}")
    if origin is not None:
        _run(repo, "update-ref", f"refs/heads/{base}", origin)
    path = _pulls_file(repo)
    if path.exists():
        path.unlink()

"""In-memory fakes for the Test Lifecycle git integration (reused by the git tasks)."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from typing import Any

from app.services.test_lifecycle import git_client as git_client_mod
from app.services.test_lifecycle.git_client import ChangedFile, GitFile, PullInfo, TlGitError

REPO_URL = "https://github.test/org/repo"


class FakeGitClient:
    """Drop-in for TlGitClient: branches, commits (file snapshots) and pull requests in memory.

    `calls` records (method, args...) tuples; `fail_once(method, status, message)` makes the
    next call of that method raise TlGitError (then the method works again).
    """

    def __init__(self, base: str = "main") -> None:
        self.base = base
        self.calls: list[tuple[Any, ...]] = []
        self.commits: dict[str, dict[str, tuple[str, str]]] = {}
        self.branches: dict[str, str] = {}
        self.pulls: dict[int, PullInfo] = {}
        self.put_messages: list[str] = []
        self._failures: dict[str, TlGitError] = {}
        self._gates: dict[str, tuple[asyncio.Event, asyncio.Event]] = {}
        self.pull_meta: dict[int, dict[str, str]] = {}
        self._n = 0
        self.branches[base] = self._commit({})

    # -- helpers -----------------------------------------------------------
    def _sha(self, seed: str) -> str:
        self._n += 1
        return hashlib.sha1(f"{seed}:{self._n}".encode()).hexdigest()  # noqa: S324

    def _commit(self, files: dict[str, tuple[str, str]]) -> str:
        sha = self._sha("commit")
        self.commits[sha] = dict(files)
        return sha

    async def _enter(self, method: str, *args: Any) -> None:
        self.calls.append((method, *args))
        gate = self._gates.get(method)
        if gate is not None:
            gate[0].set()
            await gate[1].wait()
        err = self._failures.pop(method, None)
        if err is not None:
            raise err

    def fail_once(self, method: str, status: int = 500, message: str = "boom") -> None:
        self._failures[method] = TlGitError(status, message)

    def block(self, method: str) -> tuple[asyncio.Event, asyncio.Event]:
        """Pause the next calls of `method`: returns (started, release) events."""
        gate = (asyncio.Event(), asyncio.Event())
        self._gates[method] = gate
        return gate

    def calls_of(self, method: str) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == method]

    def seed_file(self, path: str, text: str, branch: str | None = None) -> str:
        """Commit `text` at `path` on `branch` (default base) directly, bypassing call records."""
        branch = branch or self.base
        files = dict(self.commits[self.branches[branch]])
        files[path] = (text, self._sha("blob"))
        self.branches[branch] = self._commit(files)
        return self.branches[branch]

    def remove_file(self, path: str, branch: str | None = None) -> str:
        """Delete `path` on `branch` (default base) directly; returns the new head sha."""
        branch = branch or self.base
        files = dict(self.commits[self.branches[branch]])
        files.pop(path, None)
        self.branches[branch] = self._commit(files)
        return self.branches[branch]

    def merge_pull(self, number: int) -> str:
        """Merge the PR head into the base: files that differ on the head win. Returns the sha."""
        pull = self.pulls[number]
        files = dict(self.commits[self.branches[self.base]])
        for path, entry in self.commits[self.branches[pull.head_branch]].items():
            if files.get(path, (None,))[0] != entry[0]:
                files[path] = entry
        sha = self._commit(files)
        self.branches[self.base] = sha
        self.set_pull_state(number, "merged", sha)
        return sha

    def files(self, branch: str) -> dict[str, tuple[str, str]]:
        return self.commits[self.branches[branch]]

    # -- TlGitClient API ---------------------------------------------------
    async def head_sha(self, branch: str) -> str:
        await self._enter("head_sha", branch)
        if branch not in self.branches:
            raise TlGitError(404, "Not Found")
        return self.branches[branch]

    async def branch_exists(self, branch: str) -> bool:
        await self._enter("branch_exists", branch)
        return branch in self.branches

    async def create_branch(self, branch: str, from_sha: str) -> None:
        await self._enter("create_branch", branch, from_sha)
        if branch in self.branches:
            return  # idempotent, like the real client on "already exists"
        self.branches[branch] = from_sha

    async def reset_branch(self, branch: str, sha: str) -> None:
        await self._enter("reset_branch", branch, sha)
        if branch not in self.branches:
            raise TlGitError(422, "Reference does not exist")
        self.branches[branch] = sha

    async def get_file(self, path: str, ref: str) -> GitFile | None:
        await self._enter("get_file", path, ref)
        sha = self.branches.get(ref, ref)
        entry = self.commits.get(sha, {}).get(path)
        return None if entry is None else GitFile(path=path, text=entry[0], blob_sha=entry[1])

    async def put_file(
        self, path: str, text: str, branch: str, message: str, blob_sha: str | None = None
    ) -> str:
        await self._enter("put_file", path, text, branch, message, blob_sha)
        files = dict(self.commits[self.branches[branch]])
        current = files.get(path)
        if current is not None and blob_sha != current[1]:
            raise TlGitError(409, f"{path} does not match")
        files[path] = (text, self._sha("blob"))
        self.branches[branch] = self._commit(files)
        self.put_messages.append(message)
        return self.branches[branch]

    async def find_open_pull(self, head_branch: str) -> PullInfo | None:
        await self._enter("find_open_pull", head_branch)
        for pull in self.pulls.values():
            if pull.head_branch == head_branch and pull.state == "open":
                return pull
        return None

    async def create_pull(self, head: str, base: str, title: str, body: str) -> PullInfo:
        await self._enter("create_pull", head, base, title, body)
        number = 100 + len(self.pulls) + 1
        pull = PullInfo(
            number=number,
            url=f"{REPO_URL}/pull/{number}",
            state="open",
            merge_commit_sha=None,
            head_branch=head,
        )
        self.pulls[number] = pull
        self.pull_meta[number] = {"base": base, "title": title, "body": body}
        return pull

    async def get_pull(self, number: int) -> PullInfo:
        await self._enter("get_pull", number)
        return self.pulls[number]

    def set_pull_state(
        self, number: int, state: str, merge_commit_sha: str | None = None
    ) -> None:
        self.pulls[number] = replace(
            self.pulls[number], state=state, merge_commit_sha=merge_commit_sha
        )

    async def compare(self, base_sha: str, head_sha: str) -> list[ChangedFile]:
        await self._enter("compare", base_sha, head_sha)
        old = self.commits.get(base_sha, {})
        new = self.commits.get(head_sha, {})
        out: list[ChangedFile] = []
        for path in sorted(set(old) | set(new)):
            if path not in old:
                out.append(ChangedFile(path, "added"))
            elif path not in new:
                out.append(ChangedFile(path, "removed"))
            elif old[path][0] != new[path][0]:
                out.append(ChangedFile(path, "modified"))
        return out

    async def list_tree(self, ref: str) -> list[str]:
        await self._enter("list_tree", ref)
        return sorted(self.commits[self.branches.get(ref, ref)])


def install_fake_git(monkeypatch: Any, fake: FakeGitClient | None = None) -> FakeGitClient:
    """Make get_git_client() (as seen by every module) return `fake`."""
    fake = fake or FakeGitClient()
    monkeypatch.setattr(git_client_mod, "get_git_client", lambda: fake)
    return fake


def uninstall_git(monkeypatch: Any) -> None:
    """Make get_git_client() raise TlGitNotConfigured."""

    def _raise() -> None:
        raise git_client_mod.TlGitNotConfigured("Nessun token GitHub configurato")

    monkeypatch.setattr(git_client_mod, "get_git_client", _raise)

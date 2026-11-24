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

    async def delete_file(self, path: str, branch: str, message: str, blob_sha: str) -> str:
        await self._enter("delete_file", path, branch, message, blob_sha)
        files = dict(self.commits[self.branches[branch]])
        current = files.get(path)
        if current is None:
            raise TlGitError(404, f"{path} not found")
        if current[1] != blob_sha:
            raise TlGitError(409, f"{path} does not match")
        del files[path]
        self.branches[branch] = self._commit(files)
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


class FakeJiraClient:
    """Jira in memoria per i test: issue, link, utenti; registra le chiamate."""

    def __init__(self, base: str = "https://jira.test") -> None:
        from app.services.test_lifecycle.jira_client import JiraIssue

        self._Issue = JiraIssue
        self.base = base
        self.issues: dict[str, Any] = {}
        self.links: list[tuple[str, str, str]] = []
        self.created: list[dict[str, Any]] = []
        self.users: dict[str, str] = {}
        self.calls: list[tuple[Any, ...]] = []
        self._failures: dict[str, Exception] = {}
        self._gates: dict[str, tuple[asyncio.Event, asyncio.Event]] = {}
        self._next = 9001

    def issue_url(self, key: str) -> str:
        return f"{self.base}/browse/{key}"

    def add_issue(self, key: str, summary: str = "S", issue_type: str = "Story",
                  status: str = "Backlog", category: str = "new") -> None:
        self.issues[key] = self._Issue(key, summary, status, category, issue_type,
                                       key.split("-")[0], self.issue_url(key))

    def fail_once(self, method: str, status: int = 500, message: str = "boom") -> None:
        from app.services.test_lifecycle.jira_client import TlJiraError

        self._failures[method] = TlJiraError(status, message)

    def block(self, method: str) -> tuple[asyncio.Event, asyncio.Event]:
        gate = (asyncio.Event(), asyncio.Event())
        self._gates[method] = gate
        return gate

    def calls_of(self, method: str) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == method]

    async def _enter(self, method: str, *args: Any) -> None:
        self.calls.append((method, *args))
        gate = self._gates.get(method)
        if gate is not None:
            gate[0].set()
            await gate[1].wait()
        err = self._failures.pop(method, None)
        if err is not None:
            raise err

    async def get_issue(self, key: str) -> Any:
        await self._enter("get_issue", key)
        return self.issues.get(key)

    async def delete_issue(self, key: str) -> None:
        await self._enter("delete_issue", key)
        self.issues.pop(key, None)

    async def find_account_id(self, email: str) -> str | None:
        await self._enter("find_account_id", email)
        return self.users.get(email.lower())

    async def search_summary(self, project: str, prefix: str) -> Any:
        await self._enter("search_summary", project, prefix)
        for issue in self.issues.values():
            if issue.project == project and issue.summary.startswith(prefix):
                return issue
        return None

    async def search_epics(self, project: str, text: str, limit: int = 20) -> list[Any]:
        await self._enter("search_epics", project, text)
        return [i for i in self.issues.values()
                if i.project == project and i.issue_type == "Epic"
                and text.lower() in i.summary.lower()][:limit]

    async def create_issue(self, **fields: Any) -> Any:
        await self._enter("create_issue", fields)
        key = f"{fields['project']}-{self._next}"
        self._next += 1
        self.created.append({"key": key, **fields})
        self.add_issue(key, fields["summary"], fields["issue_type"])
        return self.issues[key]

    async def create_link(self, link_type: str, inward_key: str, outward_key: str) -> None:
        await self._enter("create_link", link_type, inward_key, outward_key)
        self.links.append((link_type, inward_key, outward_key))


def install_fake_jira(monkeypatch: Any, fake: FakeJiraClient | None = None) -> FakeJiraClient:
    from app.services.test_lifecycle import jira_client

    fake = fake or FakeJiraClient()
    monkeypatch.setattr(jira_client, "get_jira_client", lambda: fake)
    return fake


def uninstall_jira(monkeypatch: Any) -> None:
    from app.services.test_lifecycle import jira_client

    def _raise() -> None:
        raise jira_client.TlJiraNotConfigured("test")

    monkeypatch.setattr(jira_client, "get_jira_client", _raise)


def uninstall_confluence(monkeypatch: Any) -> None:
    from app.services.test_lifecycle import confluence_client

    def _raise() -> None:
        raise confluence_client.TlConfluenceNotConfigured("test")

    monkeypatch.setattr(confluence_client, "get_confluence_client", _raise)


class FakeConfluenceClient:
    """Confluence in memoria: pagine (id → titolo, versione, storage), utenti, scritture."""

    def __init__(self) -> None:
        from app.services.test_lifecycle import confluence_client as cc

        self._cc = cc
        self.pages: dict[str, dict[str, Any]] = {}
        self.users: dict[str, Any] = {}
        self.writes: list[tuple[str, str]] = []
        self.calls: list[tuple[Any, ...]] = []
        self._failures: dict[str, Exception] = {}

    def add_page(self, page_id: str, title: str, storage: str, version: int = 1) -> None:
        self.pages[page_id] = {"title": title, "storage": storage, "version": version}

    def add_user(self, account_id: str, name: str, email: str | None = None) -> None:
        self.users[account_id] = self._cc.AtlassianUser(account_id, name, email)

    def fail_once(self, method: str, status: int = 500, message: str = "boom") -> None:
        self._failures[method] = self._cc.TlConfluenceError(status, message)

    def _enter(self, method: str, *args: Any) -> None:
        self.calls.append((method, *args))
        err = self._failures.pop(method, None)
        if err is not None:
            raise err

    async def list_feat_pages(self, root_id: str) -> list[Any]:
        self._enter("list_feat_pages", root_id)
        return [
            self._cc.PageRef(pid, p["title"], p["version"])
            for pid, p in sorted(self.pages.items())
            if p["title"].startswith("FEAT_")
        ]

    async def get_page(self, page_id: str) -> Any:
        self._enter("get_page", page_id)
        p = self.pages[page_id]
        return self._cc.PageContent(
            page_id, p["title"], p["version"], p["storage"], f"https://wiki.test/pages/{page_id}"
        )

    async def get_user(self, account_id: str) -> Any:
        self._enter("get_user", account_id)
        return self.users.get(account_id) or self._cc.AtlassianUser(account_id, "", None)

    async def prepend_panel(self, page: Any, panel_html: str) -> int:
        from app.config import settings

        self._enter("prepend_panel", page.id)
        if not settings.tl_migration_confluence_write:
            raise self._cc.TlConfluenceError(403, "Scrittura su Confluence disattivata")
        p = self.pages[page.id]
        if self._cc.MIGRATED_MARKER in p["storage"]:
            return p["version"]
        p["storage"] = panel_html + p["storage"]
        p["version"] += 1
        self.writes.append((page.id, panel_html))
        return p["version"]


def install_fake_confluence(
    monkeypatch: Any, fake: FakeConfluenceClient | None = None
) -> FakeConfluenceClient:
    from app.services.test_lifecycle import confluence_client

    fake = fake or FakeConfluenceClient()
    monkeypatch.setattr(confluence_client, "get_confluence_client", lambda: fake)
    return fake

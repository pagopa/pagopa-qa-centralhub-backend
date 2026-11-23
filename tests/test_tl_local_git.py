# ruff: noqa: S101, S603, S607
"""Modalità Git locale: il client lavora su un clone sul disco, senza GitHub."""
from __future__ import annotations

import subprocess

import pytest

from app.config import settings
from app.services.test_lifecycle import git_client
from app.services.test_lifecycle.git_client import TlGitError
from app.services.test_lifecycle.local_git import LocalGitClient, local_merge, local_reset


def _git(repo, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.test")
    _git(origin, "config", "user.name", "T")
    (origin / "src").mkdir()
    (origin / "src" / "a.feature").write_text("# language: it\nFunzionalità: a\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "init")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    _git(clone, "switch", "-q", "-c", "feature/test-lifecycle")  # come il clone reale
    return clone


@pytest.fixture
def client(repo):
    return LocalGitClient(str(repo))


async def test_head_sha_and_missing_branch(client, repo):
    assert await client.head_sha("main") == _git(repo, "rev-parse", "main")
    with pytest.raises(TlGitError) as exc:
        await client.head_sha("nope")
    assert exc.value.status == 404


async def test_create_branch_put_file_and_read_back(client, repo):
    base = await client.head_sha("main")
    await client.create_branch("qa-hub/FEAT_001_X-r1", base)
    await client.create_branch("qa-hub/FEAT_001_X-r1", base)  # idempotente
    assert await client.branch_exists("qa-hub/FEAT_001_X-r1")
    sha = await client.put_file("features/x.feature", "# qa-hub: X\n", "qa-hub/FEAT_001_X-r1", "msg")
    assert sha == _git(repo, "rev-parse", "qa-hub/FEAT_001_X-r1")
    got = await client.get_file("features/x.feature", "qa-hub/FEAT_001_X-r1")
    assert got.text == "# qa-hub: X\n"
    assert await client.get_file("features/x.feature", "main") is None
    # aggiornare richiede lo sha del blob corrente
    with pytest.raises(TlGitError) as exc:
        await client.put_file("features/x.feature", "v2\n", "qa-hub/FEAT_001_X-r1", "m", "0" * 40)
    assert exc.value.status == 409
    await client.put_file("features/x.feature", "v2\n", "qa-hub/FEAT_001_X-r1", "m", got.blob_sha)
    assert (await client.get_file("features/x.feature", "qa-hub/FEAT_001_X-r1")).text == "v2\n"
    # nessun file toccato nella copia di lavoro del clone
    assert _git(repo, "status", "--porcelain") == ""


async def test_get_file_on_directory_raises(client):
    with pytest.raises(TlGitError) as exc:
        await client.get_file("src", "main")
    assert exc.value.status == 400


async def test_pull_lifecycle_and_merge(client, repo):
    base = await client.head_sha("main")
    branch = "qa-hub/FEAT_001_X-r1"
    await client.create_branch(branch, base)
    await client.put_file("features/x.feature", "# qa-hub: X\n", branch, "add")
    assert await client.find_open_pull(branch) is None
    pull = await client.create_pull(branch, "main", "Titolo", "Corpo")
    assert pull.number == 1 and pull.state == "open"
    assert (await client.find_open_pull(branch)).number == 1
    merge_sha = local_merge(str(repo), 1)
    merged = await client.get_pull(1)
    assert merged.state == "merged" and merged.merge_commit_sha == merge_sha
    assert await client.head_sha("main") == merge_sha
    assert (await client.get_file("features/x.feature", "main")).text == "# qa-hub: X\n"
    changed = await client.compare(base, merge_sha)
    assert [(c.path, c.status) for c in changed] == [("features/x.feature", "added")]
    assert "features/x.feature" in await client.list_tree("main")


async def test_compare_unknown_base_is_404(client):
    head = await client.head_sha("main")
    with pytest.raises(TlGitError) as exc:
        await client.compare("1" * 40, head)
    assert exc.value.status == 404


async def test_compare_reports_rename(client, repo):
    base = await client.head_sha("main")
    branch = "work"
    await client.create_branch(branch, base)
    text = (await client.get_file("src/a.feature", branch)).text
    await client.put_file("src/b.feature", text, branch, "copy")
    # rimozione del vecchio file sullo stesso branch
    _git(repo, "update-ref", f"refs/heads/{branch}", _remove(repo, branch, "src/a.feature"))
    changed = await client.compare(base, await client.head_sha(branch))
    assert [(c.path, c.status, c.previous_path) for c in changed] == [
        ("src/b.feature", "renamed", "src/a.feature")
    ]


def _remove(repo, branch, path) -> str:
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as idx:
        env = {**os.environ, "GIT_INDEX_FILE": idx.name}
    subprocess.run(["git", "-C", str(repo), "read-tree", branch], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "--cached", path], check=True, env=env)
    tree = subprocess.run(
        ["git", "-C", str(repo), "write-tree"], check=True, env=env, capture_output=True, text=True
    ).stdout.strip()
    return subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree, "-p", branch, "-m", "rm"],
        check=True, env=env, capture_output=True, text=True,
    ).stdout.strip()


async def test_reset_removes_hub_branches_and_pulls(client, repo):
    original = _git(repo, "rev-parse", "origin/main")
    branch = "qa-hub/FEAT_001_X-r1"
    await client.create_branch(branch, original)
    await client.put_file("features/x.feature", "x\n", branch, "add")
    await client.create_pull(branch, "main", "t", "b")
    local_merge(str(repo), 1)
    local_reset(str(repo))
    assert not await client.branch_exists(branch)
    assert _git(repo, "rev-parse", "main") == original
    assert await client.find_open_pull(branch) is None
    assert _git(repo, "branch", "--show-current") == "feature/test-lifecycle"


def test_get_git_client_local_mode(monkeypatch, repo):
    monkeypatch.setattr(settings, "tl_git_mode", "local")
    monkeypatch.setattr(settings, "tl_local_repo_path", str(repo))
    monkeypatch.setattr(settings, "tl_github_token", "")
    monkeypatch.setattr(settings, "tas_github_token", "")
    assert isinstance(git_client.get_git_client(), LocalGitClient)


def test_get_git_client_local_mode_requires_path(monkeypatch):
    monkeypatch.setattr(settings, "tl_git_mode", "local")
    monkeypatch.setattr(settings, "tl_local_repo_path", "")
    with pytest.raises(git_client.TlGitNotConfigured):
        git_client.get_git_client()


def test_cli_write_merge_show_reset(repo, tmp_path, capsys):
    from scripts.tl_local_git import main

    src = tmp_path / "new.feature"
    src.write_text("# language: it\nFunzionalità: b\n")
    assert main(["--repo", str(repo), "write", "main", "src/a.feature", str(src)]) == 0
    assert main(["--repo", str(repo), "show", "main", "src/a.feature"]) == 0
    assert "Funzionalità: b" in capsys.readouterr().out
    assert main(["--repo", str(repo), "merge", "7"]) == 1  # PR inesistente
    assert main(["--repo", str(repo), "rm", "main", "src/a.feature"]) == 0
    assert main(["--repo", str(repo), "show", "main", "src/a.feature"]) == 1
    assert main(["--repo", str(repo), "reset"]) == 0
    assert _git(repo, "rev-parse", "main") == _git(repo, "rev-parse", "origin/main")

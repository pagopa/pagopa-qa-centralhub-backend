# ruff: noqa: S101, F811
"""Importa da Git, Task 1: link, cancellazione di file nei client Git, provenienza."""
from __future__ import annotations

import pytest

from app.config import settings
from app.models.test_lifecycle import TlFeature
from app.services.test_lifecycle.git_client import TlGitError
from app.services.test_lifecycle.git_import_file import ImportUrlInvalid, resolve_blob
from tests.test_tl_local_git import client as local_client  # noqa: F401
from tests.test_tl_local_git import repo  # noqa: F401
from tests.tl_fakes import FakeGitClient

GH = "https://github.com/{repo}/blob/{branch}/{path}"
REPO_NAME = "pagopa/pagopa-platform-integration-test"


@pytest.fixture(autouse=True)
def _repo_setting(monkeypatch):
    monkeypatch.setattr(settings, "tl_repo", REPO_NAME)


def _url(branch: str, path: str = "src/fdr3/x.feature", repo_name: str | None = None) -> str:
    return GH.format(repo=repo_name or REPO_NAME, branch=branch, path=path)


async def test_resolve_simple_and_slashed_branch():
    git = FakeGitClient()
    git.branches["PQ-827-fdr"] = git.branches["main"]
    git.branches["feature/fdr/e2e"] = git.branches["main"]
    assert await resolve_blob(git, _url("PQ-827-fdr")) == ("PQ-827-fdr", "src/fdr3/x.feature")
    assert await resolve_blob(git, _url("feature/fdr/e2e", "a/b.feature")) == (
        "feature/fdr/e2e", "a/b.feature"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/altro/repo/blob/main/a.feature",
        f"https://github.com/{REPO_NAME}/tree/main/a.feature",
        f"https://github.com/{REPO_NAME}/blob/main/a.py",
        "ftp://github.com/x",
        f"https://github.com/{REPO_NAME}/blob/inesistente/a.feature",
    ],
)
async def test_invalid_links(url):
    with pytest.raises(ImportUrlInvalid):
        await resolve_blob(FakeGitClient(), url)


async def test_empty_repo_setting_accepts_link_repo(monkeypatch):
    monkeypatch.setattr(settings, "tl_repo", "")
    assert await resolve_blob(FakeGitClient(), _url("main")) == ("main", "src/fdr3/x.feature")


async def test_fake_delete_file():
    git = FakeGitClient()
    git.seed_file("a.feature", "x")
    blob = git.files("main")["a.feature"][1]
    await git.delete_file("a.feature", "main", "rimuovi", blob)
    assert "a.feature" not in git.files("main")
    with pytest.raises(TlGitError):
        await git.delete_file("a.feature", "main", "rimuovi", blob)


async def test_local_delete_file(local_client):
    head = await local_client.head_sha("main")
    await local_client.create_branch("qa-hub/x", head)
    await local_client.put_file("f.feature", "ciao", "qa-hub/x", "add")
    f = await local_client.get_file("f.feature", "qa-hub/x")
    await local_client.delete_file("f.feature", "qa-hub/x", "rm", f.blob_sha)
    assert await local_client.get_file("f.feature", "qa-hub/x") is None


def test_feature_has_import_source():
    assert TlFeature.__table__.c.import_source is not None

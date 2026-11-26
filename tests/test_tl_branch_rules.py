# ruff: noqa: S101
"""Working branch columns and pure sync rules (design 2026-11-26, git sync)."""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.models.test_lifecycle import TlFeatureRevision, TlScope
from app.services.test_lifecycle import branch_sync_rules as rules
from tests._db import TestSession
from tests.test_tl_features import _use_test_db  # noqa: F401  (autouse fixture)
from tests.test_tl_jira_history import _feature_with_revisions


async def test_revision_has_work_branch_columns():
    async with TestSession() as s:
        scope, _, (rev,) = await _feature_with_revisions(s)
        rev.work_branch, rev.work_file_sha = "qa-hub/FEAT_001-r1", "a" * 40
        scope_id, rev_id = scope.id, rev.id
        await s.commit()
        try:
            async with TestSession() as other:  # fresh session: proves the columns are persisted
                loaded = await other.get(TlFeatureRevision, rev_id)
                got = (loaded.work_branch, loaded.work_file_sha)
                assert got == ("qa-hub/FEAT_001-r1", "a" * 40)
        finally:
            await s.execute(delete(TlScope).where(TlScope.id == scope_id))
            await s.commit()


def test_blob_sha_matches_git():
    assert rules.blob_sha("hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
    assert rules.blob_sha("") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_blob_sha_counts_utf8_bytes_not_characters():
    # "à" is two bytes: the git header carries the byte length
    import hashlib

    data = "à\n".encode()
    expected = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324
    assert rules.blob_sha("à\n") == expected


@pytest.mark.parametrize(
    ("hub", "repo", "saved_hub", "saved_repo", "same", "expected"),
    [
        ("h", None, "h", "r", False, "file_missing"),
        ("h", None, "h", "r", True, "file_missing"),
        ("h", "r", "x", "y", True, "in_sync"),  # same content wins whatever the shas say
        ("h", "r", "h", "r", False, "in_sync"),
        ("h", "r2", "h", "r", False, "repo_ahead"),
        ("h2", "r", "h", "r", False, "hub_ahead"),
        ("h2", "r2", "h", "r", False, "diverged"),
        ("h", "r", None, None, False, "diverged"),  # nothing known about either side
        ("h", "r2", "h", None, False, "repo_ahead"),  # repo side unknown: assume it changed
        ("h2", "r", None, "r", False, "hub_ahead"),  # hub side unknown: assume it changed
    ],
)
def test_classify(hub, repo, saved_hub, saved_repo, same, expected):
    assert rules.classify(hub, repo, saved_hub, saved_repo, same) == expected


def test_default_branch_name():
    assert rules.default_branch_name("FEAT_006_POS", 2) == "qa-hub/FEAT_006_POS-r2"


@pytest.mark.parametrize("name", ["qa-hub/x-r1", "feature/abc", "fix_it-2", "a.b/c"])
def test_valid_branch_names(name):
    assert rules.check_branch_name(name, "main") is None


@pytest.mark.parametrize(
    "name",
    [
        "", "   ", "main", "a..b", "a b", "/a", "a/", "a.lock", "a~1", "a^b", "a:b", "a?b",
        "a*b", "a[b", "a\\b", "x" * 201,
        # accepted by the checks above but refused by the Git client or by git itself
        "_wip", "feature/über", "a#b", "fix,1", "x=y", ".hidden", "a/.hidden", "x.lock/y",
        "HEAD", "refs/heads/x",
    ],
)
def test_invalid_branch_names(name):
    message = rules.check_branch_name(name, "main")
    assert message and isinstance(message, str)

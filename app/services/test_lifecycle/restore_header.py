"""Restores the `# qa-hub:` header a managed feature file lost.

The pull request only prepends the header line to the file as it is on `main`: the rest of the
file, including any edit made while the header was missing, stays exactly as it is. Once merged,
the sync reads a file with its header again and Git is the truth, as for any other edit.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.test_lifecycle import TlFeature, TlIssue, TlIssueKind, TlIssueStatus
from app.schemas.test_lifecycle import TlRestoreHeaderOut
from app.services.test_lifecycle import issues as issues_svc
from app.services.test_lifecycle.actors import resolve_actor
from app.services.test_lifecycle.feature_parser import normalize_text, parse_feature_file
from app.services.test_lifecycle.git_client import TlGitError


class RestoreError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RestoreNotApplicable(RestoreError):  # noqa: N818
    """The issue is not an open lost-header question."""


class RestoreFileMissing(RestoreError):  # noqa: N818
    """The file is not on `main` any more."""


class RestoreConflict(RestoreError):  # noqa: N818
    """The file carries the header of another feature."""


def branch_for(code: str) -> str:
    return f"qa-hub/restore-header-{code.lower()}"


async def restore_header(
    db: AsyncSession, issue_id: uuid.UUID, actor_email: str, *, git: Any
) -> TlRestoreHeaderOut:
    """Opens (or refreshes) the pull request that gives the file its header back."""
    await resolve_actor(db, actor_email)  # 403 before the network
    issue = await db.get(TlIssue, issue_id)
    if issue is None:
        raise issues_svc.IssueNotFoundError(str(issue_id))
    if (
        issue.kind != TlIssueKind.MISSING_HEADER
        or issue.status == TlIssueStatus.RESOLVED
        or issue.feature_id is None
    ):
        raise RestoreNotApplicable("Questa anomalia non è un'intestazione da ripristinare.")
    feature = await db.get(TlFeature, issue.feature_id)
    code, path = feature.code, feature.git_path or issue.path
    await db.rollback()  # no transaction open while talking to Git
    if not path:
        raise RestoreFileMissing("La feature non ha un file su Git.")

    base = settings.tl_base_branch
    head = await git.head_sha(base)
    file = await git.get_file(path, head)
    if file is None:
        raise RestoreFileMissing(f"Il file {path} non è più su {base}.")
    found = parse_feature_file(normalize_text(file.text)).code
    if found == code:
        await _resolve(db, issue_id, actor_email)  # fixed by someone else in the meantime
        return TlRestoreHeaderOut()
    if found is not None:
        raise RestoreConflict(f"Il file ha già l'intestazione di un'altra feature ({found}).")

    proposed = f"# qa-hub: {code}\n" + file.text.lstrip("﻿")
    branch = branch_for(code)
    try:
        pull = await git.find_open_pull(branch)
        if await git.branch_exists(branch):
            if pull is None:
                await git.reset_branch(branch, head)  # a finished earlier attempt: start again
        else:
            await git.create_branch(branch, head)
        current = await git.get_file(path, branch)
        if current is None or current.text != proposed:
            await git.put_file(
                path,
                proposed,
                branch,
                f"chore(qa-hub): ripristina l'intestazione di {code}",
                current.blob_sha if current else None,
            )
        if pull is None:
            pull = await git.create_pull(
                branch,
                base,
                f"Ripristino intestazione QA Hub — {code}",
                f"Il file `{path}` aveva perso la riga `# qa-hub: {code}`, con cui l'hub lo "
                "riconosce.\nLa PR aggiunge solo quella riga: il resto del file non cambia.",
            )
    except TlGitError as exc:
        return TlRestoreHeaderOut(pr_error=exc.message)
    return TlRestoreHeaderOut(pr_url=pull.url)


async def _resolve(db: AsyncSession, issue_id: uuid.UUID, actor_email: str) -> None:
    issue = await db.get(TlIssue, issue_id)
    if issue is not None and issue.status != TlIssueStatus.RESOLVED:
        issue.status = TlIssueStatus.RESOLVED
        issue.updated_by = actor_email
        await db.commit()

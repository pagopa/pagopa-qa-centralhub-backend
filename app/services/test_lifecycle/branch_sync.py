"""Working-branch synchronisation of a draft's feature file with the central repository.

A revision with a working branch can be compared with the branch (`get_status`), pushed to it
(`push`, "Salva tutti") and, in the pull part of this module, replaced with the branch content.

Rules the whole module obeys: only the feature file is ever written on the branch, with a plain
commit on top of what is there (never a reset or a force push); nothing is written without the
person asking (status never writes; a repository that changed is overwritten only with
`overwrite=True`); no row lock is held during network calls (a per-revision advisory lock
keeps two syncs of the same revision apart).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from urllib.parse import quote

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.test_lifecycle import TlFeature, TlFeatureRevision, TlRevisionStatus, TlScope
from app.models.user import User
from app.schemas.test_lifecycle import TlFeatureDetail
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import git_client as git_client_mod
from app.services.test_lifecycle import git_import_content as content
from app.services.test_lifecycle import history
from app.services.test_lifecycle.actors import resolve_actor
from app.services.test_lifecycle.advisory_lock import try_advisory_lock
from app.services.test_lifecycle.branch_sync_rules import blob_sha, classify
from app.services.test_lifecycle.feature_parser import normalize_text, parse_feature_file
from app.services.test_lifecycle.gherkin import render_feature_file
from app.services.test_lifecycle.git_client import TlGitError, TlGitNotConfigured
from app.services.test_lifecycle.git_import_file import ImportFileNotFound, ImportParseError
from app.services.test_lifecycle.git_publish import commit_message, revision_file_path

logger = logging.getLogger(__name__)


class RepoChangedError(Exception):
    """The file on the branch changed since the last sync: pushing needs `overwrite=True`."""


class InvalidBranchNameError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SyncNotAllowedError(Exception):
    """Not a draft or no working branch."""


class SyncBusyError(SyncNotAllowedError):
    """Another sync of the same revision is running."""


class NoGitClient:
    """Stand-in used when no GitHub token is configured: every call says so."""

    async def _refuse(self, *args: object, **kwargs: object) -> None:
        raise TlGitNotConfigured

    branch_exists = head_sha = get_file = put_file = create_branch = _refuse


@dataclass
class BranchSync:
    branch: str | None
    state: str
    hub_unsynced: bool
    repo_changed: bool
    compare_url: str | None


@dataclass
class _Snapshot:
    """What the hub holds for a revision, read under the database and then released."""

    branch: str | None
    path: str
    text: str
    hub_sha: str
    saved_hub_sha: str | None
    saved_repo_sha: str | None
    message: str
    status: TlRevisionStatus
    number: int


def compare_url(branch: str) -> str | None:
    if not settings.tl_repo:
        return None
    return (
        f"https://github.com/{settings.tl_repo}/compare/"
        f"{quote(settings.tl_base_branch, safe='/')}...{quote(branch, safe='/')}"
    )


async def _snapshot(db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID) -> _Snapshot:
    feature = await db.get(TlFeature, feature_id)
    if feature is None:
        raise features_svc.FeatureNotFoundError(str(feature_id))
    rev = await db.get(TlFeatureRevision, revision_id)
    if rev is None or rev.feature_id != feature.id:
        raise features_svc.RevisionNotFoundError(str(revision_id))
    scope = await db.get(TlScope, feature.scope_id)
    assert scope is not None  # noqa: S101 - FK guarantees it
    render, _ = await history.build_render(db, feature, rev)
    text = render_feature_file(render)
    return _Snapshot(
        branch=rev.work_branch,
        path=revision_file_path(scope, feature, rev),
        text=text,
        hub_sha=blob_sha(text),
        saved_hub_sha=rev.work_hub_sha,
        saved_repo_sha=rev.work_file_sha,
        message=commit_message(scope, feature, rev),
        status=rev.status,
        number=rev.number,
    )


async def ensure_branch(client, branch: str, base_branch: str) -> bool:
    """Creates `branch` from the head of `base_branch` when missing. True when created."""
    if await client.branch_exists(branch):
        return False
    await client.create_branch(branch, await client.head_sha(base_branch))
    return True


async def get_status(
    db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID, client
) -> BranchSync:
    """Compares the hub with the working branch. Reads only; a Git failure is a state."""
    snap = await _snapshot(db, feature_id, revision_id)
    await db.rollback()  # no transaction open during the network
    if snap.branch is None:
        return BranchSync(None, "no_branch", False, False, None)
    link = compare_url(snap.branch)
    try:
        if not await client.branch_exists(snap.branch):
            return BranchSync(snap.branch, "branch_missing", True, False, link)
        repo = await client.get_file(snap.path, snap.branch)
    except TlGitNotConfigured:
        return BranchSync(snap.branch, "not_configured", False, False, None)
    except TlGitError:
        logger.warning("sync status: git unreachable for %s", snap.branch)
        return BranchSync(snap.branch, "unreachable", False, False, link)
    if repo is None:
        return BranchSync(snap.branch, "file_missing", True, False, link)
    same = normalize_text(repo.text) == normalize_text(snap.text)
    state = classify(snap.hub_sha, repo.blob_sha, snap.saved_hub_sha, snap.saved_repo_sha, same)
    hub_unsynced = not same and (snap.saved_hub_sha is None or snap.hub_sha != snap.saved_hub_sha)
    repo_changed = not same and (
        snap.saved_repo_sha is None or repo.blob_sha != snap.saved_repo_sha
    )
    return BranchSync(snap.branch, state, hub_unsynced, repo_changed, link)


async def push(
    db: AsyncSession,
    feature_id: uuid.UUID,
    revision_id: uuid.UUID,
    actor: User,
    client,
    *,
    overwrite: bool = False,
) -> BranchSync:
    """Commits the hub render of the draft to its working branch ("Salva tutti").

    Creates the branch from the base branch when it does not exist. Raises `RepoChangedError`
    when the file on the branch changed since the last sync, unless `overwrite`. Nothing is
    committed when the branch already holds the same content.
    """
    async with try_advisory_lock(db, f"tl_sync:{revision_id}") as got:
        if not got:
            raise SyncBusyError("busy")
        feature = await features_svc._lock_feature(db, feature_id)
        rev = await features_svc._lock_revision(db, feature_id, revision_id)
        if rev.status != TlRevisionStatus.DRAFT or rev.work_branch is None:
            raise SyncNotAllowedError(str(revision_id))
        snap = await _snapshot(db, feature_id, revision_id)
        branch = snap.branch
        assert branch is not None  # noqa: S101 - checked above
        await db.commit()  # row locks released before the network

        await ensure_branch(client, branch, settings.tl_base_branch)
        existing = await client.get_file(snap.path, branch)
        same = existing is not None and normalize_text(existing.text) == normalize_text(snap.text)
        repo_sha_after = existing.blob_sha if existing is not None else None
        wrote = False
        if not same:
            repo_changed = existing is not None and (
                snap.saved_repo_sha is None or existing.blob_sha != snap.saved_repo_sha
            )
            if repo_changed and not overwrite:
                raise RepoChangedError(branch)
            await client.put_file(
                snap.path,
                snap.text,
                branch,
                snap.message,
                existing.blob_sha if existing is not None else None,
            )
            wrote = True
            repo_sha_after = blob_sha(snap.text)

        feature = await features_svc._lock_feature(db, feature_id)
        rev = await features_svc._lock_revision(db, feature_id, revision_id)
        if rev.work_branch != branch:
            raise SyncNotAllowedError(str(revision_id))
        # The commit exists whatever the revision's state is now (a submit may have raced this
        # push): its shas are always recorded, or the next check would see a change nobody made.
        rev.work_file_sha = repo_sha_after
        rev.work_hub_sha = snap.hub_sha
        if wrote:
            history.record_event(
                db,
                feature.id,
                "tl.git.pushed",
                actor,
                {
                    "revision_id": str(rev.id),
                    "revision_number": rev.number,
                    "branch": branch,
                    "path": snap.path,
                },
            )
        await db.commit()
    return BranchSync(branch, "in_sync", False, False, compare_url(branch))


async def pull(
    db: AsyncSession,
    feature_id: uuid.UUID,
    revision_id: uuid.UUID,
    actor: User,
    client,
) -> TlFeatureDetail:
    """Replaces the draft's content with the feature file on its working branch.

    The same parser and tag rules as the Git import; scenario identities are kept (a scenario
    that is on both sides stays the same row). A file that does not parse, has no scenarios, or
    holds a scenario without a valid identity tag of this feature leaves everything untouched:
    the next push would otherwise delete that scenario from the repository.
    """
    async with try_advisory_lock(db, f"tl_sync:{revision_id}") as got:
        if not got:
            raise SyncBusyError("busy")
        feature = await features_svc._lock_feature(db, feature_id)
        rev = await features_svc._lock_revision(db, feature_id, revision_id)
        if rev.status != TlRevisionStatus.DRAFT or rev.work_branch is None:
            raise SyncNotAllowedError(str(revision_id))
        snap = await _snapshot(db, feature_id, revision_id)
        branch = snap.branch
        assert branch is not None  # noqa: S101 - checked above
        await db.commit()  # row locks released before the network

        file = await client.get_file(snap.path, branch)
        if file is None:
            raise ImportFileNotFound(snap.path)
        parsed = parse_feature_file(normalize_text(file.text))
        if parsed.errors:
            raise ImportParseError(parsed.errors)
        if not parsed.scenarios:
            raise ImportParseError(["Il file non contiene scenari"])

        feature = await features_svc._lock_feature(db, feature_id)
        rev = await features_svc._lock_revision(db, feature_id, revision_id)
        if rev.status != TlRevisionStatus.DRAFT or rev.work_branch != branch:
            raise SyncNotAllowedError(str(revision_id))
        scope = await db.get(TlScope, feature.scope_id)
        assert scope is not None  # noqa: S101 - FK guarantees it
        render, _ = await history.build_render(db, feature, rev)
        content.fill_defaults(parsed, render)
        valid = content.valid_scenarios(parsed, scope.code, feature.number)
        rejected = [sc for sc in parsed.scenarios if sc not in valid]
        if rejected or not valid:
            prefix = f"@{scope.code.upper()}_{feature.number:03d}_NN"
            reasons = [
                f"Lo scenario «{sc.title}» (riga {sc.line}) "
                f"non ha un tag identità valido ({prefix})"
                for sc in rejected
            ]
            raise ImportParseError(reasons or ["Nessuno scenario con un tag identità valido"])
        await content.apply_content(db, feature, rev, parsed, valid)
        if parsed.test_type:
            feature.test_type = parsed.test_type
        accepted, _ = await history.build_render(db, feature, rev)
        rev.work_file_sha = file.blob_sha
        rev.work_hub_sha = blob_sha(render_feature_file(accepted))
        history.record_event(
            db,
            feature.id,
            "tl.git.pulled",
            actor,
            {
                "revision_id": str(rev.id),
                "revision_number": rev.number,
                "branch": branch,
                "path": snap.path,
                "scenarios": len(valid),
            },
        )
        await db.commit()
    db.expunge_all()  # objects expired by the commit: the detail rereads them
    return await features_svc.get_feature_detail(db, feature_id, actor.email)


async def push_for_submit(
    db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID, actor_email: str
) -> None:
    """Before a draft is sent to review: the branch must hold what is reviewed.

    No working branch, no token, an unreachable Git: nothing to do and nothing blocked. A branch
    that is behind the hub (or missing) is pushed; one that changed on the repository side
    (`repo_ahead`, `diverged`) raises `RepoChangedError`: the person must accept or overwrite.
    A push of the same revision still running raises `SyncBusyError`: the submit waits for it.
    """
    try:
        snap = await _snapshot(db, feature_id, revision_id)
    except (features_svc.FeatureNotFoundError, features_svc.RevisionNotFoundError):
        return  # the transition itself reports it
    await db.rollback()
    if snap.branch is None:
        return
    try:
        client = git_client_mod.get_git_client()
    except TlGitNotConfigured:
        return
    async with try_advisory_lock(db, f"tl_sync:{revision_id}") as got:
        if not got:
            raise SyncBusyError("busy")
    status = await get_status(db, feature_id, revision_id, client)
    if status.state in ("repo_ahead", "diverged"):
        raise RepoChangedError(snap.branch)
    if status.state not in ("hub_ahead", "branch_missing", "file_missing"):
        return
    try:
        actor = await resolve_actor(db, actor_email)
        await push(db, feature_id, revision_id, actor, client)
    except (RepoChangedError, SyncBusyError):
        raise
    except (TlGitError, TlGitNotConfigured, SyncNotAllowedError):
        logger.warning("push before submit skipped for %s", snap.branch, exc_info=True)
        await db.rollback()


async def branch_in_use(db: AsyncSession, branch: str) -> bool:
    """The branch is already the working or PR branch of a revision that is still open."""
    row = (
        await db.execute(
            select(TlFeatureRevision.id)
            .where(
                or_(TlFeatureRevision.work_branch == branch, TlFeatureRevision.pr_branch == branch),
                TlFeatureRevision.status.notin_(
                    [TlRevisionStatus.MERGED, TlRevisionStatus.SUPERSEDED]
                ),
            )
            .limit(1)
        )
    ).first()
    await db.rollback()
    return row is not None


async def open_branch_best_effort(
    db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID
) -> None:
    """Opens the revision's working branch when missing and notes the repository baseline.

    The baseline is the feature file as it is on the base branch (the published one), so a
    branch that already holds other work shows up as "repo ahead". Any Git problem is only
    logged: the branch is created at the first push.
    """
    try:
        snap = await _snapshot(db, feature_id, revision_id)
    except (features_svc.FeatureNotFoundError, features_svc.RevisionNotFoundError):
        return
    await db.rollback()
    if snap.branch is None:
        return
    try:
        client = git_client_mod.get_git_client()
    except TlGitNotConfigured:
        return
    try:
        await ensure_branch(client, snap.branch, settings.tl_base_branch)
        base_file = await client.get_file(snap.path, settings.tl_base_branch)
    except (TlGitError, TlGitNotConfigured):
        logger.warning("working branch %s not opened (created at the first push)", snap.branch)
        return
    if base_file is None:
        return
    rev = await features_svc._lock_revision(db, feature_id, revision_id)
    if rev.work_branch == snap.branch and rev.work_file_sha is None:
        rev.work_file_sha = base_file.blob_sha
    await db.commit()

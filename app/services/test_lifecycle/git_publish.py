"""Pubblicazione della revisione approvata: feature file su un branch e pull request."""

from __future__ import annotations

import re
import unicodedata
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlIssue,
    TlIssueKind,
    TlIssueStatus,
    TlRevisionStatus,
    TlScope,
)
from app.models.user import User
from app.schemas.test_lifecycle import TlFeatureDetail
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import history
from app.services.test_lifecycle.gherkin import render_feature_file
from app.services.test_lifecycle.git_client import TlGitClient, TlGitError

SLUG_MAX = 60
NOT_CONFIGURED_ERROR = "Git non configurato: nessun token GitHub"


class PublishNotAllowedError(Exception):
    """La revisione non è approvata o ha già una PR aperta/mergiata."""


class RepoFolderMissingError(Exception):
    pass


def slugify(title: str) -> str:
    ascii_title = (
        unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_title)[:SLUG_MAX]
    return slug.strip("_")


def feature_path(scope: TlScope, feature: TlFeature, title: str) -> str:
    folder = (scope.repo_folder or "").strip().strip("/")
    name = f"{feature.code}_{slugify(title)}.feature"
    return f"{folder}/{name}" if folder else name


def branch_name(feature: TlFeature, revision: TlFeatureRevision) -> str:
    return f"qa-hub/{feature.code}-r{revision.number}"


def pr_title(feature: TlFeature, revision: TlFeatureRevision) -> str:
    return f"[{feature.code}] {revision.title} (r{revision.number})"


def commit_message(scope: TlScope, feature: TlFeature, revision: TlFeatureRevision) -> str:
    return f"test({scope.code}): {feature.code} r{revision.number}"


def pr_body(feature: TlFeature, revision: TlFeatureRevision, scenario_tags: list[str]) -> str:
    lines: list[str] = []
    base_url = settings.tl_hub_base_url.rstrip("/")
    if base_url:
        lines.append(f"Hub: {base_url}/test-lifecycle/{feature.id}")
    lines.append(f"Autore: {revision.created_by}")
    lines.append(f"Approvato da: {revision.approved_by or '-'}")
    lines.append("Scenari:")
    lines.extend(f"- {tag}" for tag in scenario_tags)
    return "\n".join(lines) + "\n"


def _create_issue(
    db: AsyncSession,
    feature: TlFeature,
    revision: TlFeatureRevision,
    kind: TlIssueKind,
    detail: dict,
    path: str | None = None,
) -> TlIssue:
    """Unico punto di creazione anomalie di questo modulo (sostituito da issues.open_issue)."""
    issue = TlIssue(
        feature_id=feature.id,
        revision_id=revision.id,
        kind=kind,
        status=TlIssueStatus.OPEN,
        path=path,
        detail=detail,
    )
    db.add(issue)
    return issue


async def _open_failure_issues(db: AsyncSession, revision_id: uuid.UUID) -> list[TlIssue]:
    return list(
        (
            await db.execute(
                select(TlIssue).where(
                    TlIssue.revision_id == revision_id,
                    TlIssue.kind == TlIssueKind.PR_CREATION_FAILED,
                    TlIssue.status == TlIssueStatus.OPEN,
                )
            )
        ).scalars()
    )


async def record_not_configured(db: AsyncSession, revision_id: uuid.UUID) -> None:
    """Git non configurato: l'approvazione resta valida, nessuna anomalia (non è un guasto)."""
    rev = await db.get(TlFeatureRevision, revision_id)
    if rev is not None:
        rev.pr_error = NOT_CONFIGURED_ERROR
        await db.commit()
        await db.refresh(rev)


async def publish_revision(
    db: AsyncSession,
    feature_id: uuid.UUID,
    revision_id: uuid.UUID,
    actor: User,
    client: TlGitClient,
) -> TlFeatureDetail:
    """Scrive il feature file sul branch della revisione e apre (o riusa) la PR. Idempotente.

    Su errore salva `pr_error`, apre l'anomalia `pr_creation_failed` (una sola per revisione)
    e rilancia l'eccezione originale dopo il commit.
    """
    feature = await features_svc._lock_feature(db, feature_id)
    rev = await features_svc._lock_revision(db, feature_id, revision_id)
    if rev.status != TlRevisionStatus.APPROVED or rev.pr_state not in (None, "closed"):
        raise PublishNotAllowedError(str(revision_id))
    scope = await db.get(TlScope, feature.scope_id)
    assert scope is not None  # noqa: S101 - FK guarantees it
    branch = branch_name(feature, rev)
    try:
        if not (scope.repo_folder or "").strip():
            raise RepoFolderMissingError(str(scope.id))
        path = feature.git_path or feature_path(scope, feature, rev.title)
        render, rows = await history.build_render(db, feature, rev)
        text = render_feature_file(render)
        base = settings.tl_base_branch

        base_sha = await client.head_sha(base)
        await client.create_branch(branch, base_sha)
        existing = await client.get_file(path, branch)
        if existing is None or existing.text != text:
            await client.put_file(
                path,
                text,
                branch,
                commit_message(scope, feature, rev),
                existing.blob_sha if existing else None,
            )
        pull = await client.find_open_pull(branch)
        if pull is None:
            pull = await client.create_pull(
                branch,
                base,
                pr_title(feature, rev),
                pr_body(feature, rev, [s.tag for s, _ in rows]),
            )
    except (TlGitError, RepoFolderMissingError) as exc:
        message = exc.message if isinstance(exc, TlGitError) else (
            "L'ambito non ha una cartella del repository configurata"
        )
        rev.pr_error = message
        if not await _open_failure_issues(db, rev.id):
            _create_issue(
                db, feature, rev, TlIssueKind.PR_CREATION_FAILED,
                {"error": message, "branch": branch, "revision_number": rev.number},
            )
        await db.commit()
        await db.refresh(rev)  # updated_at è calcolato dal DB: il chiamante serializza la revisione
        raise

    rev.pr_number = pull.number
    rev.pr_url = pull.url
    rev.pr_branch = branch
    rev.pr_state = "open"
    rev.pr_error = None
    rev.base_commit_sha = base_sha
    if feature.git_path is None:
        feature.git_path = path
    for issue in await _open_failure_issues(db, rev.id):
        issue.status = TlIssueStatus.RESOLVED
        issue.updated_by = actor.email
    history.record_event(
        db, feature.id, "tl.pr.created", actor,
        {
            "revision_id": str(rev.id),
            "revision_number": rev.number,
            "pr_number": pull.number,
            "pr_url": pull.url,
            "branch": branch,
            "path": path,
        },
    )
    await db.commit()
    await db.refresh(rev)
    await db.refresh(feature)
    return await features_svc.get_feature_detail(db, feature_id, actor.email)

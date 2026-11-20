"""Pubblicazione della revisione approvata: feature file su un branch e pull request."""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlIssueKind,
    TlRevisionStatus,
    TlScope,
)
from app.models.user import User
from app.schemas.test_lifecycle import TlFeatureDetail
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import history
from app.services.test_lifecycle import issues as issues_svc
from app.services.test_lifecycle.gherkin import render_feature_file
from app.services.test_lifecycle.git_client import TlGitClient, TlGitError

SLUG_MAX = 60
UNEXPECTED_ERROR = "Errore imprevisto nella creazione della PR"
logger = logging.getLogger(__name__)
NOT_CONFIGURED_ERROR = "Git non configurato: nessun token GitHub"


class PublishNotAllowedError(Exception):
    """La revisione non è approvata o ha già una PR aperta/mergiata."""


class RepoFolderMissingError(Exception):
    pass


class PublishInProgressError(Exception):
    """Un'altra richiesta sta già creando la PR di questa revisione."""


def slugify(title: str) -> str:
    ascii_title = (
        unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_title)[:SLUG_MAX]
    return slug.strip("_") or "feature"


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


async def record_not_configured(db: AsyncSession, revision_id: uuid.UUID) -> None:
    """Git non configurato: l'approvazione resta valida, nessuna anomalia (non è un guasto)."""
    rev = await db.get(TlFeatureRevision, revision_id)
    if rev is not None:
        rev.pr_error = NOT_CONFIGURED_ERROR
        await db.commit()
        await db.refresh(rev)


@asynccontextmanager
async def _pr_advisory_lock(db: AsyncSession, revision_id: uuid.UUID) -> AsyncIterator[None]:
    """Lock advisory di sessione non bloccante su una connessione dedicata.

    Una connessione propria perché la sessione ORM rilascia la sua a ogni commit, e il lock di
    sessione appartiene alla connessione. Chiudere la connessione lo rilascia comunque.
    """
    engine = db.bind
    assert engine is not None  # noqa: S101
    async with engine.connect() as conn:
        key = f"tl_pr:{revision_id}"
        got = (
            await conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": key})
        ).scalar_one()
        if not got:
            raise PublishInProgressError(str(revision_id))
        try:
            yield
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": key})


async def _record_failure(
    db: AsyncSession,
    feature_id: uuid.UUID,
    revision_id: uuid.UUID,
    message: str,
    branch: str,
) -> None:
    feature = await features_svc._lock_feature(db, feature_id)
    rev = await features_svc._lock_revision(db, feature_id, revision_id)
    rev.pr_error = message
    await issues_svc.open_issue(
        db,
        kind=TlIssueKind.PR_CREATION_FAILED,
        feature_id=feature.id,
        revision_id=rev.id,
        detail={
            "key": str(rev.id),  # al massimo un'anomalia aperta per revisione
            "error": message,
            "branch": branch,
            "revision_number": rev.number,
        },
    )
    await db.commit()
    await db.refresh(rev)  # updated_at è calcolato dal DB: il chiamante serializza la revisione


async def record_unexpected_error(db: AsyncSession, revision_id: uuid.UUID) -> None:
    await db.rollback()
    rev = await db.get(TlFeatureRevision, revision_id)
    if rev is not None:
        rev.pr_error = UNEXPECTED_ERROR
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

    Nessun lock di riga durante le chiamate GitHub: lock advisory per revisione (409 se già in
    corso), stato validato e letto sotto lock di riga, commit, chiamate di rete, poi righe
    rilockate e ricontrollate per salvare l'esito. Su errore salva `pr_error`, apre l'anomalia
    `pr_creation_failed` (una sola per revisione) e rilancia l'eccezione originale.
    """
    async with _pr_advisory_lock(db, revision_id):
        # (b) validazione e lettura sotto lock di riga
        feature = await features_svc._lock_feature(db, feature_id)
        rev = await features_svc._lock_revision(db, feature_id, revision_id)
        if rev.status != TlRevisionStatus.APPROVED or rev.pr_state not in (None, "closed"):
            raise PublishNotAllowedError(str(revision_id))
        scope = await db.get(TlScope, feature.scope_id)
        assert scope is not None  # noqa: S101 - FK guarantees it
        branch = branch_name(feature, rev)
        if not (scope.repo_folder or "").strip():
            message = "L'ambito non ha una cartella del repository configurata"
            await _record_failure(db, feature_id, revision_id, message, branch)
            raise RepoFolderMissingError(str(scope.id))
        path = feature.git_path or feature_path(scope, feature, rev.title)
        render, rows = await history.build_render(db, feature, rev)
        text_ = render_feature_file(render)
        message_ = commit_message(scope, feature, rev)
        title = pr_title(feature, rev)
        body = pr_body(feature, rev, [sc.tag for sc, _ in rows])
        known_base_sha = rev.base_commit_sha
        await db.commit()  # rilascia i lock di riga prima della rete

        # (c) GitHub, senza lock di riga
        base = settings.tl_base_branch
        try:
            base_sha = await client.head_sha(base)
            branch_existed = await client.branch_exists(branch)
            await client.create_branch(branch, base_sha)
            existing = await client.get_file(path, branch)
            if existing is None or existing.text != text_:
                await client.put_file(
                    path, text_, branch, message_, existing.blob_sha if existing else None
                )
            pull = await client.find_open_pull(branch)
            if pull is None:
                pull = await client.create_pull(branch, base, title, body)
        except TlGitError as exc:
            await _record_failure(db, feature_id, revision_id, exc.message, branch)
            raise

        # (d) esito, righe rilockate e stato ricontrollato
        feature = await features_svc._lock_feature(db, feature_id)
        rev = await features_svc._lock_revision(db, feature_id, revision_id)
        if rev.status != TlRevisionStatus.APPROVED or rev.pr_state not in (None, "closed"):
            raise PublishNotAllowedError(str(revision_id))
        rev.pr_number = pull.number
        rev.pr_url = pull.url
        rev.pr_branch = branch
        rev.pr_state = "open"
        rev.pr_error = None
        if not (branch_existed and known_base_sha):
            rev.base_commit_sha = base_sha
        if feature.git_path is None:
            feature.git_path = path
        await issues_svc.resolve_issues(
            db, kind=TlIssueKind.PR_CREATION_FAILED, revision_id=rev.id, actor=actor
        )
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

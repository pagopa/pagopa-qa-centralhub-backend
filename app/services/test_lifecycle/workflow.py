"""Workflow di progettazione della Feature (spec §6, fase 1)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureStatus,
    TlIssueKind,
    TlRevisionStatus,
    TlScope,
)
from app.models.user import User
from app.schemas.test_lifecycle import (
    TlFeatureDetail,
    TlRevisionOut,
    TlTransitionAction,
    TlTransitionOut,
)
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import git_client as git_client_mod
from app.services.test_lifecycle import git_publish, history, jira_tasks
from app.services.test_lifecycle import issues as issues_svc
from app.services.test_lifecycle import jira_client as jira_mod
from app.services.test_lifecycle import rebase as rebase_svc
from app.services.test_lifecycle import reviews as reviews_svc
from app.services.test_lifecycle.actors import resolve_actor
from app.services.test_lifecycle.git_client import TlGitError, TlGitNotConfigured
from app.services.test_lifecycle.log_safe import log_safe

logger = logging.getLogger(__name__)

REOPEN_PHASE1_REASON = rebase_svc.REOPEN_PHASE1_REASON
REVIEWER_REQUIRED_REASON = "Accesso come reviewer richiesto"
NOT_REVIEWER_REASON = "Solo il reviewer assegnato può eseguire questa azione"
NOTE_REQUIRED_REASON = "Serve una nota per richiedere modifiche"
REPO_FOLDER_REQUIRED_REASON = "L'ambito non ha una cartella del repository configurata"
REVIEWER_IS_OWNER_REASON = "Il reviewer coincide con l'owner di progettazione"
# Motivi che identificano un attore non abilitato (403) invece di una guardia bloccante (409).
_ACTOR_REASONS = (REVIEWER_REQUIRED_REASON, NOT_REVIEWER_REASON)


@dataclass
class TransitionCheck:
    action: str
    allowed: bool
    reasons: list[str] = field(default_factory=list)


class TransitionNotAllowedError(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class NotReviewerError(Exception):
    pass


def _submit_reasons(
    feature: TlFeature,
    revision: TlFeatureRevision,
    view: TlRevisionOut,
    scope: TlScope | None = None,
) -> list[str]:
    if revision.status != TlRevisionStatus.DRAFT:
        return ["La revisione non è in bozza"]
    reasons: list[str] = []
    if revision.base_stale:
        reasons.append(rebase_svc.STALE_REASON)
    if feature.design_owner_id is None:
        reasons.append("Manca l'owner di progettazione")
    if feature.reviewer_id is None:
        reasons.append("Manca il reviewer")
    elif feature.reviewer_id == feature.design_owner_id:
        reasons.append(REVIEWER_IS_OWNER_REASON)
    if not feature.jira_design_key:
        reasons.append("Manca la chiave Jira di progettazione")
    if not revision.product_issue_key and not revision.reverse_engineering:
        reasons.append("Manca l'Epic/Story prodotto (o il flag reverse engineering)")
    if scope is not None and not (scope.repo_folder or "").strip():
        reasons.append(REPO_FOLDER_REQUIRED_REASON)
    if not view.scenarios:
        reasons.append("Serve almeno uno scenario")
    for sc in view.scenarios:
        if sc.errors:
            reasons.append(f"Lo scenario {sc.tag} ha step non validi")
    if view.background_errors:
        reasons.append("Il Contesto contiene errori")
    return reasons


def _review_reasons(
    feature: TlFeature, revision: TlFeatureRevision, actor: User | None
) -> list[str]:
    if revision.status != TlRevisionStatus.IN_REVIEW:
        return ["La revisione non è in revisione"]
    if actor is None:
        return [REVIEWER_REQUIRED_REASON]
    if feature.reviewer_id != actor.id:
        return [NOT_REVIEWER_REASON]
    return []


def available_transitions(
    feature: TlFeature,
    revision: TlFeatureRevision | None,
    actor: User | None,
    view: TlRevisionOut | None = None,
    scope: TlScope | None = None,
) -> list[TlTransitionOut]:
    """Le 4 azioni, sempre nello stesso ordine, con `allowed` e motivi in italiano.

    `view` è la revisione serializzata (scenari con errori di validazione); serve a `submit`,
    insieme a `scope` (cartella del repository obbligatoria).
    """
    if revision is None or view is None:
        no_rev = ["Nessuna revisione di lavoro aperta"]
        checks = [
            TransitionCheck(TlTransitionAction.SUBMIT.value, False, list(no_rev)),
            TransitionCheck(TlTransitionAction.APPROVE.value, False, list(no_rev)),
            TransitionCheck(TlTransitionAction.REQUEST_CHANGES.value, False, list(no_rev)),
        ]
    else:
        submit = _submit_reasons(feature, revision, view, scope)
        review = _review_reasons(feature, revision, actor)
        approve = list(review)
        if not approve and feature.reviewer_id == feature.design_owner_id:
            # Difesa in profondità: l'owner non può approvare il proprio lavoro.
            approve = [REVIEWER_IS_OWNER_REASON]
        if not approve and revision.base_stale:
            approve = [rebase_svc.STALE_REASON]
        checks = [
            TransitionCheck(TlTransitionAction.SUBMIT.value, not submit, submit),
            TransitionCheck(TlTransitionAction.APPROVE.value, not approve, approve),
            TransitionCheck(TlTransitionAction.REQUEST_CHANGES.value, not review, list(review)),
        ]
    reopen = rebase_svc.reopen_reasons(feature)
    checks.append(TransitionCheck(TlTransitionAction.REOPEN.value, not reopen, reopen))
    return [
        TlTransitionOut(action=TlTransitionAction(c.action), allowed=c.allowed, reasons=c.reasons)
        for c in checks
    ]


async def apply_transition(
    db: AsyncSession,
    feature_id: uuid.UUID,
    revision_id: uuid.UUID,
    action: TlTransitionAction,
    actor_email: str,
    note: str | None,
) -> TlFeatureDetail:
    actor = await resolve_actor(db, actor_email)
    feature = await features_svc._lock_feature(db, feature_id)
    rev = await features_svc._lock_revision(db, feature_id, revision_id)

    if action == TlTransitionAction.REOPEN:
        reasons = rebase_svc.reopen_reasons(feature)
        if reasons:
            raise TransitionNotAllowedError(reasons)
        await rebase_svc.reopen(db, feature, actor)
        await db.commit()
        return await features_svc.get_feature_detail(db, feature_id, actor.email)

    view = await features_svc._revision_out(db, rev)
    scope = await db.get(TlScope, feature.scope_id)
    check = next(
        c for c in available_transitions(feature, rev, actor, view, scope) if c.action == action
    )
    if not check.allowed:
        # Revisione nello stato giusto ma attore non reviewer: 403, non 409.
        if (
            action in (TlTransitionAction.APPROVE, TlTransitionAction.REQUEST_CHANGES)
            and rev.status == TlRevisionStatus.IN_REVIEW
            and any(r in _ACTOR_REASONS for r in check.reasons)
        ):
            raise NotReviewerError(actor.email)
        raise TransitionNotAllowedError(check.reasons)

    first_publication = feature.published_revision_id is None
    if action == TlTransitionAction.SUBMIT:
        rev.status = TlRevisionStatus.IN_REVIEW
        if first_publication:
            feature.status = TlFeatureStatus.IN_REVIEW
        history.record_event(
            db,
            feature.id,
            "tl.revision.submitted",
            actor,
            {"revision_id": str(rev.id), "revision_number": rev.number},
        )
    elif action == TlTransitionAction.REQUEST_CHANGES:
        text = (note or "").strip()
        if not text:
            raise TransitionNotAllowedError([NOTE_REQUIRED_REASON])
        rev.status = TlRevisionStatus.DRAFT
        if first_publication:
            feature.status = TlFeatureStatus.DRAFT
        reviews_svc.create_comment(db, feature.id, rev, actor, text, emit_event=False)
        history.record_event(
            db,
            feature.id,
            "tl.revision.changes_requested",
            actor,
            {"revision_id": str(rev.id), "revision_number": rev.number, "note": text},
        )
    else:  # APPROVE
        rev.status = TlRevisionStatus.APPROVED
        rev.approved_by = actor.email
        rev.approved_at = datetime.now(UTC)
        if first_publication:
            feature.status = TlFeatureStatus.DESIGNED
        version = await history.snapshot(db, feature, rev, actor)
        history.record_event(
            db,
            feature.id,
            "tl.revision.approved",
            actor,
            {
                "revision_id": str(rev.id),
                "revision_number": rev.number,
                "version_id": str(version.id),
            },
        )
    await db.commit()
    await db.refresh(rev)
    actor_email = actor.email  # gli step successivi fanno rollback: l'oggetto scade
    if action == TlTransitionAction.APPROVE:
        await _publish_best_effort(db, feature_id, revision_id, actor)
        await _import_cleanup_best_effort(db, feature_id)
        await _jira_impl_best_effort(db, feature_id, actor_email)
    return await features_svc.get_feature_detail(db, feature_id, actor_email)


async def _publish_best_effort(
    db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID, actor: User
) -> None:
    """Dopo l'approvazione già committata: se la PR fallisce l'approvazione resta valida.

    L'errore è registrato in `pr_error` (e nell'anomalia, per i guasti); mai propagato.
    """
    try:
        client = git_client_mod.get_git_client()
    except TlGitNotConfigured:
        await git_publish.record_not_configured(db, revision_id)
        return
    try:
        await git_publish.publish_revision(
            db, feature_id, revision_id, actor, client, allow_open=True
        )
    except (TlGitError, git_publish.RepoFolderMissingError):
        pass  # già registrato da publish_revision
    except Exception:
        # l'approvazione è già committata: mai trasformarla in un 500
        logger.exception("Creazione PR fallita per la revisione %s", log_safe(revision_id))
        await git_publish.record_unexpected_error(db, revision_id)


async def _import_cleanup_best_effort(db: AsyncSession, feature_id: uuid.UUID) -> None:
    """Feature importata da un branch: PR di pulizia dopo la PR dell'hub. Mai propagato."""
    from app.services.test_lifecycle import git_import_file

    try:
        client = git_client_mod.get_git_client()
        await git_import_file.open_cleanup_pr(db, feature_id, client)
    except TlGitNotConfigured:
        return
    except Exception:
        logger.exception("PR di pulizia non creata per la feature %s", log_safe(feature_id))
        await db.rollback()


async def _jira_impl_best_effort(db: AsyncSession, feature_id: uuid.UUID, actor_email: str) -> None:
    """Dopo l'approvazione: crea il task Jira di implementazione se manca. Mai propagato.

    Jira non configurato -> solo `jira_error`; errore Jira -> `jira_error` e anomalia
    `jira_creation_failed` (una sola aperta per feature), risolta quando il task viene creato.
    """
    feature = await db.get(TlFeature, feature_id, populate_existing=True)
    if feature is None or feature.jira_impl_key:
        return
    await db.rollback()
    try:
        client = jira_mod.get_jira_client()
    except jira_mod.TlJiraNotConfigured:
        feature = await features_svc._lock_feature(db, feature_id)
        feature.jira_error = "Jira non configurato"
        await db.commit()
        return
    try:
        await jira_tasks.create_task(db, feature_id, "impl", actor_email, client)
    except jira_tasks.JiraTaskExists:
        return
    except Exception as exc:  # l'approvazione è già salvata: mai un 500
        await db.rollback()
        message = exc.message if isinstance(exc, jira_mod.TlJiraError) else str(exc)
        logger.warning(
            "Task Jira di implementazione non creato per %s: %s",
            log_safe(feature_id),
            log_safe(message),
        )
        feature = await features_svc._lock_feature(db, feature_id)
        feature.jira_error = message or "Creazione del task Jira non riuscita"
        await issues_svc.open_issue(
            db,
            kind=TlIssueKind.JIRA_CREATION_FAILED,
            feature_id=feature_id,
            detail={"key": "impl", "message": feature.jira_error},
        )
        await db.commit()

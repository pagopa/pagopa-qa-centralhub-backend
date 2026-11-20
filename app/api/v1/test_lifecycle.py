from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from app.deps import DbDep
from app.models.test_lifecycle import TlFeatureStatus, TlIssueKind, TlIssueStatus
from app.schemas.test_lifecycle import (
    TlCommentCreate,
    TlCommentOut,
    TlCommentResolve,
    TlFeatureCreate,
    TlFeatureDetail,
    TlFeatureListItem,
    TlFeatureUpdate,
    TlGherkinValidateOut,
    TlGherkinValidateRequest,
    TlHistoryItem,
    TlIssueOut,
    TlIssueUpdate,
    TlPreviewOut,
    TlPrRequest,
    TlRevisionOut,
    TlRevisionUpdate,
    TlScenarioCreate,
    TlScenarioOut,
    TlScenarioUpdate,
    TlScopeCreate,
    TlScopeOut,
    TlScopeUpdate,
    TlTransitionRequest,
    TlVersionOut,
)
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import git_client as git_client_mod
from app.services.test_lifecycle import git_publish as git_publish_svc
from app.services.test_lifecycle import history as history_svc
from app.services.test_lifecycle import issues as issues_svc
from app.services.test_lifecycle import reviews as reviews_svc
from app.services.test_lifecycle import scopes as scopes_svc
from app.services.test_lifecycle import workflow as workflow_svc
from app.services.test_lifecycle.actors import UnknownActorError, resolve_actor
from app.services.test_lifecycle.git_client import TlGitError, TlGitNotConfigured

router = APIRouter()

# Nessun endpoint di cancellazione per gli ambiti: le FK delle feature sono ON DELETE CASCADE.


@router.get("/scopes", response_model=list[TlScopeOut])
async def list_scopes(db: DbDep) -> list[TlScopeOut]:
    return await scopes_svc.list_scopes(db)


@router.post("/scopes", response_model=TlScopeOut, status_code=status.HTTP_201_CREATED)
async def create_scope(body: TlScopeCreate, db: DbDep) -> TlScopeOut:
    try:
        scope = await scopes_svc.create_scope(db, body)
    except scopes_svc.ScopeCodeConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "scope_code_conflict",
                "message": "Esiste già un ambito con questo codice",
            },
        ) from exc
    return await scopes_svc.get_scope_out(db, scope)


@router.patch("/scopes/{scope_id}", response_model=TlScopeOut)
async def update_scope(scope_id: uuid.UUID, body: TlScopeUpdate, db: DbDep) -> TlScopeOut:
    try:
        scope = await scopes_svc.update_scope(db, scope_id, body)
    except scopes_svc.ScopeNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Ambito non trovato") from exc
    except scopes_svc.ScopeCodeLockedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "scope_code_locked",
                "message": "Il codice dell'ambito non può cambiare: esistono già feature",
            },
        ) from exc
    except scopes_svc.ScopeCodeConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "scope_code_conflict",
                "message": "Esiste già un ambito con questo codice",
            },
        ) from exc
    return await scopes_svc.get_scope_out(db, scope)


# ── Feature, revisioni, scenari ───────────────────────────────────────────────

def _translate(exc: Exception) -> HTTPException:
    """Traduce le eccezioni di dominio del servizio feature in errori HTTP."""
    if isinstance(exc, UnknownActorError):
        return HTTPException(
            status_code=403,
            detail={"code": "unknown_actor", "message": "Utente non riconosciuto o non attivo"},
        )
    if isinstance(exc, issues_svc.IssueNotFoundError):
        return HTTPException(status_code=404, detail="Anomalia non trovata")
    if isinstance(exc, features_svc.FeatureNotFoundError):
        return HTTPException(status_code=404, detail="Feature non trovata")
    if isinstance(exc, features_svc.RevisionNotFoundError):
        return HTTPException(status_code=404, detail="Revisione non trovata")
    if isinstance(exc, features_svc.ScenarioNotFoundError):
        return HTTPException(status_code=404, detail="Scenario non trovato")
    if isinstance(exc, scopes_svc.ScopeNotFoundError):
        return HTTPException(status_code=404, detail="Ambito non trovato")
    if isinstance(exc, features_svc.UnknownUserError):
        return HTTPException(
            status_code=422,
            detail={
                "code": "unknown_user",
                "message": "Utente indicato non esistente o non attivo",
            },
        )
    if isinstance(exc, features_svc.InvalidAssignmentError):
        return HTTPException(
            status_code=422, detail={"code": exc.code, "message": exc.message}
        )
    if isinstance(exc, features_svc.StaleLockError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "stale_lock",
                "message": "La revisione è stata modificata da qualcun altro: ricarica e riprova",
            },
        )
    if isinstance(exc, features_svc.RevisionLockedError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "revision_locked",
                "message": f"La revisione non è modificabile nello stato {exc.status.value}",
            },
        )
    if isinstance(exc, history_svc.VersionNotFoundError):
        return HTTPException(status_code=404, detail="Versione non trovata")
    if isinstance(exc, workflow_svc.TransitionNotAllowedError):
        return HTTPException(
            status_code=409,
            detail={"code": "transition_not_allowed", "reasons": exc.reasons},
        )
    if isinstance(exc, workflow_svc.NotReviewerError):
        return HTTPException(
            status_code=403,
            detail={
                "code": "not_reviewer",
                "message": "Solo il reviewer assegnato può approvare o richiedere modifiche",
            },
        )
    if isinstance(exc, reviews_svc.CommentNotFoundError):
        return HTTPException(status_code=404, detail="Commento non trovato")
    if isinstance(exc, reviews_svc.RevisionNotCommentableError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "revision_not_commentable",
                "message": (
                    "La revisione non accetta commenti nello stato "
                    f"{exc.status.value}: si commenta solo in bozza o in revisione"
                ),
            },
        )
    if isinstance(exc, TlGitNotConfigured):
        return HTTPException(
            status_code=503,
            detail={
                "code": "git_not_configured",
                "message": "Integrazione Git non configurata: manca il token GitHub",
            },
        )
    if isinstance(exc, TlGitError):
        return HTTPException(
            status_code=502, detail={"code": "github_error", "message": exc.message}
        )
    if isinstance(exc, git_publish_svc.PublishNotAllowedError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "pr_not_allowed",
                "message": (
                    "La PR si può (ri)creare solo per una revisione approvata "
                    "senza PR aperta o mergiata"
                ),
            },
        )
    if isinstance(exc, git_publish_svc.PublishInProgressError):
        return HTTPException(
            status_code=409,
            detail={"code": "pr_in_progress", "message": "Creazione della PR già in corso"},
        )
    if isinstance(exc, git_publish_svc.RepoFolderMissingError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "repo_folder_missing",
                "message": "L'ambito non ha una cartella del repository configurata",
            },
        )
    raise exc


@router.get("/features", response_model=list[TlFeatureListItem])
async def list_features(
    db: DbDep,
    scope_id: uuid.UUID | None = None,
    status: TlFeatureStatus | None = None,
    owner_id: uuid.UUID | None = None,
) -> list[TlFeatureListItem]:
    return await features_svc.list_features(db, scope_id, status, owner_id)


@router.post("/features", response_model=TlFeatureDetail, status_code=status.HTTP_201_CREATED)
async def create_feature(body: TlFeatureCreate, db: DbDep) -> TlFeatureDetail:
    try:
        feature_id = await features_svc.create_feature(db, body)
        return await features_svc.get_feature_detail(db, feature_id)
    except Exception as exc:
        raise _translate(exc) from exc


@router.get("/features/{feature_id}", response_model=TlFeatureDetail)
async def get_feature(
    feature_id: uuid.UUID, db: DbDep, actor_email: str | None = None
) -> TlFeatureDetail:
    try:
        return await features_svc.get_feature_detail(db, feature_id, actor_email)
    except Exception as exc:
        raise _translate(exc) from exc


@router.patch("/features/{feature_id}", response_model=TlFeatureDetail)
async def update_feature(
    feature_id: uuid.UUID, body: TlFeatureUpdate, db: DbDep
) -> TlFeatureDetail:
    try:
        return await features_svc.update_feature(db, feature_id, body)
    except Exception as exc:
        raise _translate(exc) from exc


@router.patch("/features/{feature_id}/revisions/{revision_id}", response_model=TlRevisionOut)
async def update_revision(
    feature_id: uuid.UUID, revision_id: uuid.UUID, body: TlRevisionUpdate, db: DbDep
) -> TlRevisionOut:
    try:
        return await features_svc.update_revision(db, feature_id, revision_id, body)
    except Exception as exc:
        raise _translate(exc) from exc


@router.post(
    "/features/{feature_id}/revisions/{revision_id}/scenarios",
    response_model=TlScenarioOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_scenario(
    feature_id: uuid.UUID, revision_id: uuid.UUID, body: TlScenarioCreate, db: DbDep
) -> TlScenarioOut:
    try:
        return await features_svc.add_scenario(db, feature_id, revision_id, body)
    except Exception as exc:
        raise _translate(exc) from exc


@router.patch(
    "/features/{feature_id}/revisions/{revision_id}/scenarios/{scenario_id}",
    response_model=TlScenarioOut,
)
async def update_scenario(
    feature_id: uuid.UUID,
    revision_id: uuid.UUID,
    scenario_id: uuid.UUID,
    body: TlScenarioUpdate,
    db: DbDep,
) -> TlScenarioOut:
    try:
        return await features_svc.update_scenario(db, feature_id, revision_id, scenario_id, body)
    except Exception as exc:
        raise _translate(exc) from exc


@router.delete(
    "/features/{feature_id}/revisions/{revision_id}/scenarios/{scenario_id}",
    response_model=TlRevisionOut,
)
async def remove_scenario(
    feature_id: uuid.UUID,
    revision_id: uuid.UUID,
    scenario_id: uuid.UUID,
    actor_email: str,
    db: DbDep,
) -> TlRevisionOut:
    """Toglie lo scenario dalla revisione; il numero resta consumato. Ritorna la revisione."""
    try:
        return await features_svc.remove_scenario(
            db, feature_id, revision_id, scenario_id, actor_email
        )
    except Exception as exc:
        raise _translate(exc) from exc


@router.post(
    "/features/{feature_id}/revisions/{revision_id}/transitions",
    response_model=TlFeatureDetail,
)
async def transition_revision(
    feature_id: uuid.UUID, revision_id: uuid.UUID, body: TlTransitionRequest, db: DbDep
) -> TlFeatureDetail:
    """Transizione di workflow. Bloccata -> 409 con `reasons`; non reviewer -> 403.

    `request_changes` senza nota è bloccata con 409 `transition_not_allowed` (nessuna
    validazione di schema: la nota è obbligatoria solo per quell'azione). Una nota oltre
    5000 caratteri (come i commenti) è rifiutata con 422 dallo schema.
    """
    try:
        return await workflow_svc.apply_transition(
            db, feature_id, revision_id, body.action, body.actor_email, body.note
        )
    except Exception as exc:
        raise _translate(exc) from exc


@router.post(
    "/features/{feature_id}/revisions/{revision_id}/pr",
    response_model=TlFeatureDetail,
)
async def publish_revision_pr(
    feature_id: uuid.UUID, revision_id: uuid.UUID, body: TlPrRequest, db: DbDep
) -> TlFeatureDetail:
    """(Ri)prova la creazione di branch, feature file e PR (permesso UI manage:tl_git o
    edit:tl_feature). Senza token 503; errore GitHub 502 (salvato anche in `pr_error`)."""
    try:
        actor = await resolve_actor(db, body.actor_email)
        client = git_client_mod.get_git_client()
        return await git_publish_svc.publish_revision(db, feature_id, revision_id, actor, client)
    except Exception as exc:
        raise _translate(exc) from exc


@router.get("/features/{feature_id}/history", response_model=list[TlHistoryItem])
async def feature_history(feature_id: uuid.UUID, db: DbDep) -> list[TlHistoryItem]:
    try:
        await features_svc.get_feature_detail(db, feature_id)
        return await history_svc.list_history(db, feature_id)
    except Exception as exc:
        raise _translate(exc) from exc


@router.get("/features/{feature_id}/versions/{version_id}", response_model=TlVersionOut)
async def get_version(feature_id: uuid.UUID, version_id: uuid.UUID, db: DbDep) -> TlVersionOut:
    try:
        return await history_svc.get_version(db, feature_id, version_id)
    except Exception as exc:
        raise _translate(exc) from exc


# ── Commenti di review, anteprima, validazione live ──────────────────────────

@router.get(
    "/features/{feature_id}/revisions/{revision_id}/comments",
    response_model=list[TlCommentOut],
)
async def list_comments(
    feature_id: uuid.UUID, revision_id: uuid.UUID, db: DbDep
) -> list[TlCommentOut]:
    try:
        return await reviews_svc.list_comments(db, feature_id, revision_id)
    except Exception as exc:
        raise _translate(exc) from exc


@router.post(
    "/features/{feature_id}/revisions/{revision_id}/comments",
    response_model=TlCommentOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_comment(
    feature_id: uuid.UUID, revision_id: uuid.UUID, body: TlCommentCreate, db: DbDep
) -> TlCommentOut:
    try:
        return await reviews_svc.add_comment(db, feature_id, revision_id, body)
    except Exception as exc:
        raise _translate(exc) from exc


@router.patch("/comments/{comment_id}", response_model=TlCommentOut)
async def resolve_comment(
    comment_id: uuid.UUID, body: TlCommentResolve, db: DbDep
) -> TlCommentOut:
    try:
        return await reviews_svc.set_resolved(db, comment_id, body.actor_email, body.resolved)
    except Exception as exc:
        raise _translate(exc) from exc


@router.get(
    "/features/{feature_id}/revisions/{revision_id}/preview", response_model=TlPreviewOut
)
async def preview_revision(
    feature_id: uuid.UUID, revision_id: uuid.UUID, db: DbDep
) -> TlPreviewOut:
    try:
        return await reviews_svc.preview(db, feature_id, revision_id)
    except Exception as exc:
        raise _translate(exc) from exc


@router.post("/gherkin/validate", response_model=TlGherkinValidateOut)
async def validate_gherkin(body: TlGherkinValidateRequest) -> TlGherkinValidateOut:
    """Validazione live di un blocco di step: pura, nessuna dipendenza dal DB."""
    return TlGherkinValidateOut(errors=reviews_svc.validate_block(body))


@router.get("/issues", response_model=list[TlIssueOut])
async def list_issues(
    db: DbDep,
    status: TlIssueStatus | None = None,
    feature_id: uuid.UUID | None = None,
    kind: TlIssueKind | None = None,
) -> list[TlIssueOut]:
    return await issues_svc.list_issues(db, status, feature_id, kind)


@router.patch("/issues/{issue_id}", response_model=TlIssueOut)
async def update_issue(issue_id: uuid.UUID, body: TlIssueUpdate, db: DbDep) -> TlIssueOut:
    try:
        return await issues_svc.update_issue(
            db, issue_id, TlIssueStatus(body.status), body.actor_email
        )
    except Exception as exc:
        raise _translate(exc) from exc

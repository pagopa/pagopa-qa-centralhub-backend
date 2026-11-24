from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import case, select

from app.config import settings
from app.deps import DbDep
from app.models.test_lifecycle import (
    TlFeatureStatus,
    TlGitSyncState,
    TlIssueKind,
    TlIssueStatus,
    TlScope,
    TlStepPhrase,
)
from app.schemas.test_lifecycle import (
    TlCommentCreate,
    TlCommentOut,
    TlCommentResolve,
    TlDeletePreview,
    TlDeleteRequest,
    TlDeleteResult,
    TlFeatureCreate,
    TlFeatureDetail,
    TlFeatureListItem,
    TlFeatureNrt,
    TlFeatureUpdate,
    TlGherkinValidateOut,
    TlGherkinValidateRequest,
    TlGitStatusOut,
    TlGitSyncSummaryOut,
    TlHistoryItem,
    TlImportGitRequest,
    TlImportGitResult,
    TlIssueOut,
    TlIssueUpdate,
    TlJiraActorRequest,
    TlJiraLink,
    TlNrtRunLink,
    TlPreviewOut,
    TlPrRequest,
    TlRebaseApply,
    TlRebaseView,
    TlRevisionOut,
    TlRevisionUpdate,
    TlScenarioCreate,
    TlScenarioOut,
    TlScenarioUpdate,
    TlScopeCreate,
    TlScopeOut,
    TlScopeUpdate,
    TlStepPhraseOut,
    TlTransitionRequest,
    TlVersionOut,
)
from app.services.test_lifecycle import deletion as deletion_svc
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import git_client as git_client_mod
from app.services.test_lifecycle import git_import_file
from app.services.test_lifecycle import git_publish as git_publish_svc
from app.services.test_lifecycle import git_sync as git_sync_svc
from app.services.test_lifecycle import history as history_svc
from app.services.test_lifecycle import issues as issues_svc
from app.services.test_lifecycle import jira_client as jira_client_mod
from app.services.test_lifecycle import jira_tasks as jira_tasks_svc
from app.services.test_lifecycle import nrt as nrt_svc
from app.services.test_lifecycle import rebase as rebase_svc
from app.services.test_lifecycle import reviews as reviews_svc
from app.services.test_lifecycle import scopes as scopes_svc
from app.services.test_lifecycle import workflow as workflow_svc
from app.services.test_lifecycle.actors import UnknownActorError, resolve_actor
from app.services.test_lifecycle.category_tags import UnknownExtraTagError, category_tags
from app.services.test_lifecycle.git_client import TlGitError, TlGitNotConfigured

router = APIRouter()


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
    except (jira_tasks_svc.JiraIssueNotFound, jira_tasks_svc.JiraNotAnEpic) as exc:
        raise _translate(exc) from exc
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
    except (jira_tasks_svc.JiraIssueNotFound, jira_tasks_svc.JiraNotAnEpic) as exc:
        raise _translate(exc) from exc
    return await scopes_svc.get_scope_out(db, scope)


@router.get("/scopes/{scope_id}/delete-preview", response_model=TlDeletePreview)
async def scope_delete_preview(scope_id: uuid.UUID, db: DbDep) -> TlDeletePreview:
    try:
        return await deletion_svc.preview_scope(db, scope_id)
    except Exception as exc:
        raise _translate(exc) from exc


@router.delete("/scopes/{scope_id}", response_model=TlDeleteResult)
async def delete_scope(scope_id: uuid.UUID, body: TlDeleteRequest, db: DbDep) -> TlDeleteResult:
    try:
        return await deletion_svc.delete_scope(db, scope_id, body)
    except Exception as exc:
        raise _translate(exc) from exc


# ── Feature, revisioni, scenari ───────────────────────────────────────────────

def _translate(exc: Exception) -> HTTPException:
    """Traduce le eccezioni di dominio del servizio feature in errori HTTP."""
    if isinstance(exc, UnknownActorError):
        return HTTPException(
            status_code=403,
            detail={"code": "unknown_actor", "message": "Utente non riconosciuto o non attivo"},
        )
    if isinstance(exc, issues_svc.IssueAlreadyResolvedError):
        return HTTPException(
            status_code=409,
            detail={"code": "issue_already_resolved", "message": "L'anomalia è già risolta"},
        )
    if isinstance(exc, issues_svc.IssueNotFoundError):
        return HTTPException(status_code=404, detail="Anomalia non trovata")
    if isinstance(exc, git_import_file.ImportUrlInvalid):
        return HTTPException(
            status_code=422, detail={"code": "import_url_invalid", "message": exc.message}
        )
    if isinstance(exc, git_import_file.ImportFileNotFound):
        return HTTPException(
            status_code=404,
            detail={"code": "import_file_not_found", "message": f"File non trovato: {exc}"},
        )
    if isinstance(exc, git_import_file.ImportParseError):
        return HTTPException(
            status_code=422,
            detail={
                "code": "import_parse_error",
                "message": "Il feature file non è leggibile",
                "reasons": exc.reasons,
            },
        )
    if isinstance(exc, UnknownExtraTagError):
        return HTTPException(
            status_code=422,
            detail={
                "code": "unknown_extra_tag",
                "message": f"Tag non ammessi: {', '.join(exc.tags)}. "
                f"Ammessi: {', '.join(exc.allowed)}",
            },
        )
    if isinstance(exc, deletion_svc.FeatureNotDeletableError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "feature_not_deletable",
                "message": "La feature non può essere eliminata",
                "blockers": [b.model_dump(mode="json") for b in exc.blockers],
            },
        )
    if isinstance(exc, deletion_svc.ScopeNotDeletableError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "scope_not_deletable",
                "message": "L'ambito non può essere eliminato",
                "blockers": [b.model_dump(mode="json") for b in exc.blockers],
            },
        )
    if isinstance(exc, deletion_svc.JiraKeyNotLinkedError):
        return HTTPException(
            status_code=422,
            detail={
                "code": "jira_key_not_linked",
                "message": f"Issue Jira non collegate: {', '.join(exc.keys)}",
            },
        )
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
    if isinstance(exc, jira_tasks_svc.JiraIssueNotFound):
        return HTTPException(
            status_code=422,
            detail={
                "code": "jira_issue_not_found",
                "message": f"L'issue Jira {exc.key} non esiste",
                "key": exc.key,
            },
        )
    if isinstance(exc, jira_tasks_svc.JiraNotAnEpic):
        return HTTPException(
            status_code=422,
            detail={
                "code": "jira_not_an_epic",
                "message": f"{exc.key} non è un'Epic del progetto {settings.tl_jira_project}",
                "key": exc.key,
            },
        )
    if isinstance(exc, jira_tasks_svc.JiraInProgress):
        return HTTPException(
            status_code=409,
            detail={"code": "jira_in_progress", "message": "Creazione su Jira già in corso"},
        )
    if isinstance(exc, jira_tasks_svc.JiraTaskExists):
        return HTTPException(
            status_code=409,
            detail={
                "code": "jira_task_exists",
                "message": f"Già collegato a {exc.key}",
                "key": exc.key,
            },
        )
    if isinstance(exc, jira_tasks_svc.JiraTaskNotAllowed):
        return HTTPException(
            status_code=409, detail={"code": "jira_task_not_allowed", "message": exc.reason}
        )
    if isinstance(exc, jira_client_mod.TlJiraNotConfigured):
        return HTTPException(
            status_code=503,
            detail={"code": "jira_not_configured", "message": "Jira non è configurato"},
        )
    if isinstance(exc, jira_client_mod.TlJiraError):
        return HTTPException(status_code=502, detail={"code": "jira_error", "message": exc.message})
    if isinstance(exc, rebase_svc.RebaseRequiredError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "rebase_required",
                "message": "Scegli un lato per ogni elemento in conflitto",
                "conflicts": exc.conflicts,
            },
        )
    if isinstance(exc, rebase_svc.RebaseNotNeededError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "rebase_not_needed",
                "message": "La bozza non è basata su una versione superata",
            },
        )
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
    if isinstance(exc, git_sync_svc.SyncInProgress):
        return HTTPException(
            status_code=409,
            detail={
                "code": "sync_in_progress",
                "message": "Sincronizzazione con Git già in corso",
            },
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


@router.get("/features/{feature_id}/delete-preview", response_model=TlDeletePreview)
async def feature_delete_preview(feature_id: uuid.UUID, db: DbDep) -> TlDeletePreview:
    try:
        return await deletion_svc.preview_feature(db, feature_id)
    except Exception as exc:
        raise _translate(exc) from exc


@router.delete("/features/{feature_id}", response_model=TlDeleteResult)
async def delete_feature(feature_id: uuid.UUID, body: TlDeleteRequest, db: DbDep) -> TlDeleteResult:
    try:
        return await deletion_svc.delete_feature(db, feature_id, body)
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


@router.post(
    "/features/{feature_id}/revisions/{revision_id}/import-git",
    response_model=TlImportGitResult,
)
async def import_git(
    feature_id: uuid.UUID, revision_id: uuid.UUID, body: TlImportGitRequest, db: DbDep
) -> TlImportGitResult:
    """Sostituisce gli scenari della bozza con quelli di un feature file su Git (ogni branch)."""
    try:
        client = git_client_mod.get_git_client()
        return await git_import_file.import_from_git(
            db, feature_id, revision_id, body.url, body.lock_version, body.actor_email, client
        )
    except Exception as exc:
        raise _translate(exc) from exc


@router.post("/features/{feature_id}/import-cleanup", response_model=TlFeatureDetail)
async def retry_import_cleanup(
    feature_id: uuid.UUID, body: TlJiraActorRequest, db: DbDep
) -> TlFeatureDetail:
    """Riprova la PR che toglie il file importato dal branch di partenza."""
    try:
        await resolve_actor(db, body.actor_email)
        client = git_client_mod.get_git_client()
        await git_import_file.open_cleanup_pr(db, feature_id, client, force=True)
        db.expunge_all()
        return await features_svc.get_feature_detail(db, feature_id, body.actor_email)
    except Exception as exc:
        raise _translate(exc) from exc


@router.get("/category-tags", response_model=list[str])
async def list_category_tags(db: DbDep) -> list[str]:
    """Tag di categoria ammessi sugli scenari."""
    return list(await category_tags(db))


@router.get("/features/{feature_id}/nrt", response_model=TlFeatureNrt)
async def get_feature_nrt(feature_id: uuid.UUID, db: DbDep) -> TlFeatureNrt:
    """Esiti NRT per scenario: ultimo per ambiente, pass rate, flaky."""
    try:
        return await nrt_svc.feature_nrt(db, feature_id)
    except Exception as exc:
        raise _translate(exc) from exc


@router.get("/nrt/runs/{run_id}/links", response_model=list[TlNrtRunLink])
async def get_run_links(run_id: uuid.UUID, db: DbDep) -> list[TlNrtRunLink]:
    """Esecuzioni di una run NRT collegate a feature dell'hub."""
    return await nrt_svc.run_feature_links(db, run_id)


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


async def _jira_task(
    db: DbDep, feature_id: uuid.UUID, kind: str, actor_email: str
) -> TlFeatureDetail:
    await resolve_actor(db, actor_email)  # 403 prima di chiamare Jira
    client = jira_client_mod.get_jira_client()
    warning = await jira_tasks_svc.create_task(db, feature_id, kind, actor_email, client)
    detail = await features_svc.get_feature_detail(db, feature_id, actor_email)
    detail.jira_warning = warning
    return detail


@router.post("/features/{feature_id}/jira/design-task", response_model=TlFeatureDetail)
async def create_jira_design_task(
    feature_id: uuid.UUID, body: TlJiraActorRequest, db: DbDep
) -> TlFeatureDetail:
    """Crea (o ritrova) il task Jira di progettazione: revisione in bozza, chiave vuota."""
    try:
        return await _jira_task(db, feature_id, "design", body.actor_email)
    except Exception as exc:
        raise _translate(exc) from exc


@router.post("/features/{feature_id}/jira/impl-task", response_model=TlFeatureDetail)
async def create_jira_impl_task(
    feature_id: uuid.UUID, body: TlJiraActorRequest, db: DbDep
) -> TlFeatureDetail:
    """Crea (o ritrova) il task Jira di implementazione: feature approvata, chiave vuota."""
    try:
        return await _jira_task(db, feature_id, "impl", body.actor_email)
    except Exception as exc:
        raise _translate(exc) from exc


@router.post("/scopes/{scope_id}/jira/epic", response_model=TlScopeOut)
async def create_scope_jira_epic(
    scope_id: uuid.UUID, body: TlJiraActorRequest, db: DbDep
) -> TlScopeOut:
    """Crea (o ritrova per titolo) l'Epic QA dell'ambito su Jira e la collega."""
    try:
        await resolve_actor(db, body.actor_email)
        client = jira_client_mod.get_jira_client()
        await jira_tasks_svc.create_scope_epic(db, scope_id, client)
        scope = await db.get(TlScope, scope_id)
        return await scopes_svc.get_scope_out(db, scope)
    except Exception as exc:
        raise _translate(exc) from exc


@router.get("/jira/epics", response_model=list[TlJiraLink])
async def search_jira_epics(q: str = "") -> list[TlJiraLink]:
    """Epic del progetto QA per il selettore dell'ambito (testo vuoto: le più recenti)."""
    try:
        client = jira_client_mod.get_jira_client()
        epics = await client.search_epics(settings.tl_jira_project, q)
    except Exception as exc:
        raise _translate(exc) from exc
    return [
        TlJiraLink(
            role="qa_epic", key=e.key, summary=e.summary, status=e.status,
            category=e.status_category, url=e.url, verified=True,
        )
        for e in epics
    ]


@router.post("/features/{feature_id}/jira/refresh", response_model=TlFeatureDetail)
async def refresh_jira(
    feature_id: uuid.UUID, body: TlJiraActorRequest, db: DbDep
) -> TlFeatureDetail:
    """Rilegge da Jira lo stato dei task collegati (Jira non configurato -> 503)."""
    try:
        await resolve_actor(db, body.actor_email)  # 403 prima di chiamare Jira
        client = jira_client_mod.get_jira_client()
        await jira_tasks_svc.refresh_feature(db, feature_id, body.actor_email, client)
        return await features_svc.get_feature_detail(db, feature_id, body.actor_email)
    except Exception as exc:
        raise _translate(exc) from exc


@router.get(
    "/features/{feature_id}/revisions/{revision_id}/rebase",
    response_model=TlRebaseView,
)
async def get_rebase(feature_id: uuid.UUID, revision_id: uuid.UUID, db: DbDep) -> TlRebaseView:
    """Confronto a tre vie (base / Git / bozza) per metadati e scenari."""
    try:
        return await rebase_svc.get_rebase_view(db, feature_id, revision_id)
    except Exception as exc:
        raise _translate(exc) from exc


@router.post(
    "/features/{feature_id}/revisions/{revision_id}/rebase",
    response_model=TlFeatureDetail,
)
async def apply_rebase(
    feature_id: uuid.UUID, revision_id: uuid.UUID, body: TlRebaseApply, db: DbDep
) -> TlFeatureDetail:
    """Riallinea la bozza alla versione pubblicata. Conflitti senza scelta -> 409
    `rebase_required` con l'elenco; bozza non superata -> 409 `rebase_not_needed`."""
    try:
        return await rebase_svc.apply_rebase(db, feature_id, revision_id, body)
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


@router.post("/git/sync", response_model=TlGitSyncSummaryOut)
async def git_sync(body: TlPrRequest, db: DbDep) -> TlGitSyncSummaryOut:
    """Sincronizza ora (permesso UI manage:tl_git). 409 se già in corso, 503 senza token,
    502 per errori GitHub (salvato anche in `last_error`)."""
    try:
        await resolve_actor(db, body.actor_email)
        client = git_client_mod.get_git_client()
        summary = await git_sync_svc.run_sync(db, client)
        return TlGitSyncSummaryOut(**asdict(summary))
    except Exception as exc:
        raise _translate(exc) from exc


@router.get("/git/status", response_model=TlGitStatusOut)
async def git_status(db: DbDep) -> TlGitStatusOut:
    try:
        git_client_mod.get_git_client()
        configured = True
    except TlGitNotConfigured:
        configured = False
    state = await db.get(TlGitSyncState, 1)
    if state is None:
        return TlGitStatusOut(configured=configured)
    return TlGitStatusOut(
        configured=configured,
        last_main_sha=state.last_main_sha,
        last_run_at=state.last_run_at,
        last_error=state.last_error,
        last_summary=state.last_summary,
    )


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


@router.get("/steps", response_model=list[TlStepPhraseOut])
async def search_steps(
    db: DbDep,
    q: str = "",
    keyword: Literal["given", "when", "then"] | None = None,
    limit: int = Query(10, ge=1, le=50),
) -> list[TlStepPhraseOut]:
    """Autocompletamento step: prefisso, poi sottostringa; a parità implementati e più usati."""
    q = q.strip()
    stmt = select(TlStepPhrase)
    order = []
    if q:
        stmt = stmt.where(TlStepPhrase.text.icontains(q, autoescape=True))
        order.append(case((TlStepPhrase.text.istartswith(q, autoescape=True), 0), else_=1))
    if keyword:
        stmt = stmt.where(TlStepPhrase.keyword == keyword)
    stmt = stmt.order_by(
        *order,
        TlStepPhrase.implemented.desc(),
        TlStepPhrase.occurrences.desc(),
        TlStepPhrase.text,
    ).limit(limit)
    return [TlStepPhraseOut.model_validate(r) for r in (await db.execute(stmt)).scalars()]

"""Integrazione Jira del Test Lifecycle (spec §8, fase 3).

- verifica delle chiavi inserite (esistono? l'Epic QA è un'Epic del progetto QA?);
- cache dello stato dei task collegati sulla Feature (`jira_cache`), mostrata nel dettaglio;
- creazione dei task Progettazione/Implementazione e dell'Epic QA di un Ambito.

Le chiamate a Jira non avvengono mai con lock di riga tenuti. Jira non configurato o non
raggiungibile non blocca il salvataggio di una chiave: resta "non verificata". Solo un 404
certo (o un'Epic che non è un'Epic) la rifiuta.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.test_lifecycle import TlFeature, TlFeatureRevision, TlScope
from app.models.user import User
from app.schemas.test_lifecycle import TlJiraLink
from app.services.test_lifecycle import history
from app.services.test_lifecycle import jira_client as jira_mod
from app.services.test_lifecycle.actors import resolve_actor
from app.services.test_lifecycle.jira_client import JiraIssue, TlJiraError, TlJiraNotConfigured

logger = logging.getLogger(__name__)


class JiraIssueNotFound(Exception):  # noqa: N818 - nome di dominio
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class JiraNotAnEpic(Exception):  # noqa: N818
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _entry(issue: JiraIssue) -> dict[str, Any]:
    return {
        "summary": issue.summary,
        "status": issue.status,
        "category": issue.status_category,
        "issue_type": issue.issue_type,
        "project": issue.project,
        "url": issue.url,
        "verified": True,
        "checked_at": datetime.now(UTC).isoformat(),
    }


def _unverified() -> dict[str, Any]:
    return {"verified": False, "checked_at": datetime.now(UTC).isoformat()}


def _is_epic(issue: JiraIssue) -> bool:
    return (
        issue.issue_type == settings.tl_jira_epic_issue_type
        and issue.project == settings.tl_jira_project
    )


async def verify_key(key: str, *, epic: bool = False) -> dict[str, Any]:
    """Voce di cache per `key`. Solleva JiraIssueNotFound / JiraNotAnEpic se certo."""
    try:
        client = jira_mod.get_jira_client()
        issue = await client.get_issue(key)
    except (TlJiraNotConfigured, TlJiraError) as exc:
        logger.info("Verifica Jira di %s non eseguita: %s", key, exc)
        return _unverified()
    if issue is None:
        raise JiraIssueNotFound(key)
    if epic and not _is_epic(issue):
        raise JiraNotAnEpic(key)
    return _entry(issue)


async def verify_keys(
    keys: dict[str, str | None], *, epics: set[str] = frozenset()
) -> dict[str, Any]:
    """Verifica le chiavi non nulle `{campo: chiave}`; restituisce `{chiave: voce}`."""
    out: dict[str, Any] = {}
    for field, key in keys.items():
        if key and key not in out:
            out[key] = await verify_key(key, epic=field in epics)
    return out


def merge_cache(feature: TlFeature, entries: dict[str, Any]) -> None:
    if entries:
        feature.jira_cache = {**(feature.jira_cache or {}), **entries}


# ── dettaglio ─────────────────────────────────────────────────────────────────


def effective_epic(feature: TlFeature, scope: TlScope) -> str | None:
    return feature.jira_qa_epic_key or scope.jira_qa_epic_key


def jira_links(feature: TlFeature, scope: TlScope, product_key: str | None) -> list[TlJiraLink]:
    cache = feature.jira_cache or {}
    roles = [
        ("design", feature.jira_design_key),
        ("impl", feature.jira_impl_key),
        ("qa_epic", effective_epic(feature, scope)),
        ("product", product_key),
    ]
    links: list[TlJiraLink] = []
    for role, key in roles:
        if not key:
            continue
        entry = cache.get(key) or {}
        links.append(
            TlJiraLink(
                role=role,
                key=key,
                summary=entry.get("summary"),
                status=entry.get("status"),
                category=entry.get("category"),
                url=entry.get("url")
                or (
                    f"{settings.jira_base_url.rstrip('/')}/browse/{key}"
                    if settings.jira_base_url
                    else None
                ),
                verified=bool(entry.get("verified")),
            )
        )
    return links


async def _product_key(db: AsyncSession, feature: TlFeature) -> str | None:
    rev_id = feature.working_revision_id or feature.published_revision_id
    if rev_id is None:
        return None
    return (
        await db.execute(
            select(TlFeatureRevision.product_issue_key).where(TlFeatureRevision.id == rev_id)
        )
    ).scalar_one_or_none()


# ── aggiornamento ─────────────────────────────────────────────────────────────


async def linked_keys(db: AsyncSession, feature: TlFeature) -> list[str]:
    scope = await db.get(TlScope, feature.scope_id)
    keys = [
        feature.jira_design_key,
        feature.jira_impl_key,
        effective_epic(feature, scope) if scope else feature.jira_qa_epic_key,
        await _product_key(db, feature),
    ]
    return list(dict.fromkeys(k for k in keys if k))


def _comparable(entry: dict[str, Any] | None) -> tuple[Any, ...]:
    entry = entry or {}
    return tuple(entry.get(k) for k in ("summary", "status", "category", "verified"))


async def refresh_feature(
    db: AsyncSession, feature_id: uuid.UUID, actor_email: str | None, client: Any
) -> bool:
    """Rilegge da Jira le chiavi collegate e aggiorna la cache. True se è cambiato qualcosa.

    Errori sulla singola chiave: la voce resta com'è. Evento solo se cambia qualcosa.
    """
    # import locale: features importa questo modulo
    from app.services.test_lifecycle import features as features_svc

    feature = await db.get(TlFeature, feature_id)
    if feature is None:
        raise features_svc.FeatureNotFoundError(str(feature_id))
    keys = await linked_keys(db, feature)
    await db.rollback()  # nessuna transazione aperta durante le chiamate a Jira
    fresh: dict[str, Any] = {}
    for key in keys:
        try:
            issue = await client.get_issue(key)
        except TlJiraError as exc:
            logger.info("Aggiornamento Jira di %s fallito: %s", key, exc)
            continue
        fresh[key] = (
            _entry(issue)
            if issue is not None
            else {"verified": False, "missing": True, "checked_at": datetime.now(UTC).isoformat()}
        )
    # attore risolto dopo la rete: la rollback precedente scade gli oggetti caricati
    actor = await resolve_actor(db, actor_email) if actor_email else None
    feature = await features_svc._lock_feature(db, feature_id)
    cache = dict(feature.jira_cache or {})
    changed = [k for k, v in fresh.items() if _comparable(cache.get(k)) != _comparable(v)]
    cache.update(fresh)
    feature.jira_cache = cache
    if changed:
        history.record_event(db, feature.id, "tl.jira.refreshed", actor, {"keys": changed})
    await db.commit()
    return bool(changed)


# ── creazione di task ed Epic QA ──────────────────────────────────────────────


class JiraInProgress(Exception):  # noqa: N818
    """Un'altra richiesta sta già creando lo stesso elemento Jira."""


class JiraTaskExists(Exception):  # noqa: N818
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class JiraTaskNotAllowed(Exception):  # noqa: N818
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_KIND_LABEL = {"design": "Progettazione", "impl": "Implementazione suite"}


def _issue_type(kind: str) -> str:
    return (
        settings.tl_jira_design_issue_type if kind == "design" else settings.tl_jira_impl_issue_type
    )


def task_summary(code: str, kind: str, title: str) -> str:
    return f"[{code}] {_KIND_LABEL[kind]} — {title}"[:255]


def _description(feature_id: uuid.UUID, tags: list[str]) -> dict[str, Any]:
    paragraphs: list[dict[str, Any]] = []
    if settings.tl_hub_base_url:
        url = f"{settings.tl_hub_base_url.rstrip('/')}/test-lifecycle/{feature_id}"
        paragraphs.append(
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Feature progettata su QA Hub: "},
                    {
                        "type": "text",
                        "text": url,
                        "marks": [{"type": "link", "attrs": {"href": url}}],
                    },
                ],
            }
        )
    if tags:
        paragraphs.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Scenari: " + " ".join(tags)}],
            }
        )
    return {"type": "doc", "version": 1, "content": paragraphs}


async def _feature_lock(db: AsyncSession, feature_id: uuid.UUID) -> Any:
    # import locale: evita il ciclo con advisory_lock/features
    from app.services.test_lifecycle.advisory_lock import try_advisory_lock

    return try_advisory_lock(db, f"tl_jira:{feature_id}")


async def create_task(
    db: AsyncSession, feature_id: uuid.UUID, kind: str, actor_email: str, client: Any
) -> str | None:
    """Crea (o ritrova) il task Jira `kind` ("design"|"impl") e lo collega alla feature.

    Restituisce l'eventuale avviso da mostrare (assegnatario non trovato, link non creato).
    Nessuna rete con lock di riga: advisory lock per feature, lettura breve, rete, rilock.
    """
    from app.services.test_lifecycle import features as features_svc

    field = "jira_design_key" if kind == "design" else "jira_impl_key"
    async with await _feature_lock(db, feature_id) as got:
        if not got:
            raise JiraInProgress(str(feature_id))
        # lettura sotto lock di riga
        feature = await features_svc._lock_feature(db, feature_id)
        if getattr(feature, field):
            raise JiraTaskExists(getattr(feature, field))
        rev_id = feature.working_revision_id or feature.published_revision_id
        rev = await db.get(TlFeatureRevision, rev_id) if rev_id else None
        if kind == "design":
            if rev is None or feature.working_revision_id is None or rev.status.value != "draft":
                raise JiraTaskNotAllowed(
                    "Il task di progettazione si crea con la revisione in bozza"
                )
            owner_id = feature.design_owner_id
        else:
            if feature.status.value not in ("designed", "implemented", "running"):
                raise JiraTaskNotAllowed("Il task di implementazione si crea dopo l'approvazione")
            owner_id = feature.impl_owner_id
        scope = await db.get(TlScope, feature.scope_id)
        owner = await db.get(User, owner_id) if owner_id else None
        tags = (
            [sc.tag for sc, _ in (await history.build_render(db, feature, rev))[1]] if rev else []
        )
        params = {
            "code": feature.code,
            "title": rev.title if rev else feature.code,
            "epic": effective_epic(feature, scope) if scope else feature.jira_qa_epic_key,
            "component": scope.jira_component if scope else None,
            "product": rev.product_issue_key if rev else None,
            "owner_email": owner.email if owner else None,
        }
        await db.commit()  # rilascia i lock di riga prima della rete

        warnings: list[str] = []
        summary = task_summary(params["code"], kind, params["title"])
        try:
            issue = await client.search_summary(settings.tl_jira_project, summary)
            reused = issue is not None
            if issue is None:
                assignee = None
                if params["owner_email"]:
                    assignee = await client.find_account_id(params["owner_email"])
                    if assignee is None:
                        email = params["owner_email"]
                        warnings.append(f"Utente {email} non trovato su Jira: task non assegnato")
                issue = await client.create_issue(
                    project=settings.tl_jira_project,
                    issue_type=_issue_type(kind),
                    summary=summary,
                    description_adf=_description(feature_id, tags),
                    labels=[settings.tl_jira_label],
                    parent_key=params["epic"],
                    component=params["component"],
                    assignee_id=assignee,
                )
            if params["product"] and not reused:
                try:
                    await client.create_link(
                        settings.tl_jira_link_type, issue.key, params["product"]
                    )
                except TlJiraError as exc:
                    warnings.append(f"Link a {params['product']} non creato: {exc.message}")
        except TlJiraError as exc:
            feature = await features_svc._lock_feature(db, feature_id)
            feature.jira_error = exc.message
            await db.commit()
            raise

        # esito, riga rilockata
        actor = await resolve_actor(db, actor_email)
        feature = await features_svc._lock_feature(db, feature_id)
        if getattr(feature, field) is None:
            setattr(feature, field, issue.key)
        merge_cache(feature, {issue.key: _entry(issue)})
        feature.jira_error = None
        if kind == "impl":
            from app.models.test_lifecycle import TlIssueKind
            from app.services.test_lifecycle import issues as issues_svc

            await issues_svc.resolve_feature_issues(
                db, kind=TlIssueKind.JIRA_CREATION_FAILED, feature_id=feature_id
            )
        history.record_event(
            db,
            feature.id,
            "tl.jira.task_linked" if reused else "tl.jira.task_created",
            actor,
            {"kind": kind, "key": issue.key, "summary": issue.summary},
        )
        await db.commit()
    return "; ".join(warnings) or None


def epic_summary(scope_name: str) -> str:
    return f"[QA Epic] {scope_name}"[:255]


async def create_scope_epic(db: AsyncSession, scope_id: uuid.UUID, client: Any) -> str:
    """Crea (o ritrova per titolo) l'Epic QA dell'ambito e ne salva la chiave."""
    from app.services.test_lifecycle.advisory_lock import try_advisory_lock
    from app.services.test_lifecycle.scopes import ScopeNotFoundError

    async with try_advisory_lock(db, f"tl_jira_scope:{scope_id}") as got:
        if not got:
            raise JiraInProgress(str(scope_id))
        scope = await db.get(TlScope, scope_id, with_for_update=True, populate_existing=True)
        if scope is None:
            raise ScopeNotFoundError(str(scope_id))
        if scope.jira_qa_epic_key:
            raise JiraTaskExists(scope.jira_qa_epic_key)
        summary = epic_summary(scope.name)
        component = scope.jira_component
        await db.commit()

        issue = await client.search_summary(settings.tl_jira_project, summary)
        if issue is None or issue.issue_type != settings.tl_jira_epic_issue_type:
            issue = await client.create_issue(
                project=settings.tl_jira_project,
                issue_type=settings.tl_jira_epic_issue_type,
                summary=summary,
                description_adf=None,
                labels=[settings.tl_jira_label],
                parent_key=None,
                component=component,
                assignee_id=None,
            )
        scope = await db.get(TlScope, scope_id, with_for_update=True, populate_existing=True)
        if scope is not None and not scope.jira_qa_epic_key:
            scope.jira_qa_epic_key = issue.key
        await db.commit()
        logger.info("Epic QA %s collegata all'ambito %s", issue.key, scope_id)
    return issue.key

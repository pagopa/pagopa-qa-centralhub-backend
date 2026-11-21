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

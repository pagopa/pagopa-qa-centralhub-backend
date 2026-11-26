"""Delete previews and deletion of Test Lifecycle features and scopes.

A feature is deletable only if it has never reached Git (no published revision, no Git file) and
its working revision has no open pull request. A scope is deletable only if all its features are.
The DB rows go first (FK cascades cover revisions, scenarios, history, comments and anomalies);
the Jira issues the user selected are deleted afterwards, best effort, and reported per key.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Literal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.test_lifecycle import TlFeature, TlFeatureRevision, TlScenario, TlScope
from app.schemas.test_lifecycle import (
    TlDeleteAffectedFeature,
    TlDeleteBlocker,
    TlDeleteJiraIssue,
    TlDeletePreview,
    TlDeleteRequest,
    TlDeleteResult,
    TlJiraDeleteResult,
)
from app.services.test_lifecycle import jira_client as jira_client_mod
from app.services.test_lifecycle.actors import resolve_actor
from app.services.test_lifecycle.features import FeatureNotFoundError
from app.services.test_lifecycle.jira_client import TlJiraError
from app.services.test_lifecycle.log_safe import log_safe
from app.services.test_lifecycle.scopes import ScopeNotFoundError

logger = logging.getLogger(__name__)


class FeatureNotDeletableError(Exception):
    def __init__(self, blockers: list[TlDeleteBlocker]) -> None:
        super().__init__("feature not deletable")
        self.blockers = blockers


class ScopeNotDeletableError(Exception):
    def __init__(self, blockers: list[TlDeleteBlocker]) -> None:
        super().__init__("scope not deletable")
        self.blockers = blockers


class JiraKeyNotLinkedError(Exception):
    def __init__(self, keys: list[str]) -> None:
        super().__init__("jira keys not linked")
        self.keys = keys


async def _blockers(db: AsyncSession, features: list[TlFeature]) -> list[TlDeleteBlocker]:
    working_ids = [f.working_revision_id for f in features if f.working_revision_id]
    open_pr: set[uuid.UUID] = set()
    if working_ids:
        open_pr = set(
            (
                await db.execute(
                    select(TlFeatureRevision.id).where(
                        TlFeatureRevision.id.in_(working_ids),
                        TlFeatureRevision.pr_state == "open",
                    )
                )
            ).scalars()
        )
    out: list[TlDeleteBlocker] = []
    for f in features:
        if f.published_revision_id or f.git_path:
            out.append(TlDeleteBlocker(feature_id=f.id, code=f.code, reason="published"))
        if f.working_revision_id in open_pr:
            out.append(TlDeleteBlocker(feature_id=f.id, code=f.code, reason="pr_open"))
    return out


def _issue(
    key: str,
    role: Literal["design", "impl", "qa_epic"],
    cache: dict[str, dict],
    feature_code: str | None,
) -> TlDeleteJiraIssue:
    entry = cache.get(key) or {}
    url = entry.get("url") or (
        f"{settings.jira_base_url.rstrip('/')}/browse/{key}" if settings.jira_base_url else None
    )
    return TlDeleteJiraIssue(
        key=key,
        role=role,
        summary=entry.get("summary"),
        status=entry.get("status"),
        url=url,
        feature_code=feature_code,
        default_selected=role != "qa_epic",
    )


async def _build_preview(
    db: AsyncSession, features: list[TlFeature], epic_key: str | None
) -> TlDeletePreview:
    blockers = await _blockers(db, features)
    feature_ids = [f.id for f in features]

    rev_ids = [f.working_revision_id or f.published_revision_id for f in features]
    titles: dict[uuid.UUID, str] = {}
    if any(rev_ids):
        titles = dict(
            (
                await db.execute(
                    select(TlFeatureRevision.id, TlFeatureRevision.title).where(
                        TlFeatureRevision.id.in_([r for r in rev_ids if r])
                    )
                )
            ).all()
        )
    scenario_count = 0
    if feature_ids:
        scenario_count = (
            await db.execute(
                select(func.count()).select_from(TlScenario).where(TlScenario.feature_id.in_(feature_ids))
            )
        ).scalar_one()

    issues: dict[str, TlDeleteJiraIssue] = {}
    cache: dict[str, dict] = {}
    for f in features:
        cache.update(f.jira_cache or {})
    older: dict[uuid.UUID, list[tuple[str | None, str | None]]] = {}
    if feature_ids:  # keys of every revision, not only the current one
        for fid, design, impl in (
            await db.execute(
                select(
                    TlFeatureRevision.feature_id,
                    TlFeatureRevision.jira_design_key,
                    TlFeatureRevision.jira_impl_key,
                )
                .where(TlFeatureRevision.feature_id.in_(feature_ids))
                .order_by(TlFeatureRevision.number)
            )
        ).all():
            older.setdefault(fid, []).append((design, impl))
    for f in features:
        pairs = [(f.jira_design_key, f.jira_impl_key), *older.get(f.id, [])]
        for design, impl in pairs:
            for key, role in ((design, "design"), (impl, "impl")):
                if key and key not in issues:
                    issues[key] = _issue(key, role, f.jira_cache or {}, f.code)
    if epic_key and epic_key not in issues:
        issues[epic_key] = _issue(epic_key, "qa_epic", cache, None)

    return TlDeletePreview(
        deletable=not blockers,
        blockers=blockers,
        features=[
            TlDeleteAffectedFeature(
                id=f.id,
                code=f.code,
                title=titles.get(f.working_revision_id or f.published_revision_id),
            )
            for f in features
        ],
        scenario_count=scenario_count,
        jira_issues=list(issues.values()),
    )


async def _scope_features(
    db: AsyncSession, scope_id: uuid.UUID, *, lock: bool = False
) -> list[TlFeature]:
    stmt = select(TlFeature).where(TlFeature.scope_id == scope_id).order_by(TlFeature.number)
    if lock:
        # publishing locks the feature, not the scope: lock them so a PR cannot open mid-delete
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return list((await db.execute(stmt)).scalars())


async def preview_feature(db: AsyncSession, feature_id: uuid.UUID) -> TlDeletePreview:
    feature = await db.get(TlFeature, feature_id)
    if feature is None:
        raise FeatureNotFoundError(str(feature_id))
    return await _build_preview(db, [feature], None)


async def preview_scope(db: AsyncSession, scope_id: uuid.UUID) -> TlDeletePreview:
    scope = await db.get(TlScope, scope_id)
    if scope is None:
        raise ScopeNotFoundError(str(scope_id))
    return await _build_preview(db, await _scope_features(db, scope_id), scope.jira_qa_epic_key)


def _selected_keys(preview: TlDeletePreview, requested: list[str]) -> list[str]:
    keys = list(dict.fromkeys(requested))
    offered = {i.key for i in preview.jira_issues}
    unknown = [k for k in keys if k not in offered]
    if unknown:
        raise JiraKeyNotLinkedError(unknown)
    return keys


async def _delete_jira(client: object, keys: list[str]) -> list[TlJiraDeleteResult]:
    results: list[TlJiraDeleteResult] = []
    for key in keys:
        try:
            await client.delete_issue(key)  # type: ignore[attr-defined]
        except TlJiraError as exc:
            results.append(TlJiraDeleteResult(key=key, status="failed", reason=exc.message))
        else:
            results.append(TlJiraDeleteResult(key=key, status="deleted"))
    return results


def _audit(actor_id: uuid.UUID, kind: str, entity_id: uuid.UUID, detail: dict) -> AuditLog:
    return AuditLog(
        actor_id=actor_id,
        action=f"tl_{kind}_deleted",
        resource_type=f"tl_{kind}",
        resource_id=str(entity_id),
        detail=json.dumps(detail),
    )


async def delete_feature(
    db: AsyncSession, feature_id: uuid.UUID, data: TlDeleteRequest
) -> TlDeleteResult:
    actor = await resolve_actor(db, data.actor_email)
    feature = (
        await db.execute(
            select(TlFeature)
            .where(TlFeature.id == feature_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if feature is None:
        raise FeatureNotFoundError(str(feature_id))
    preview = await _build_preview(db, [feature], None)
    if preview.blockers:
        raise FeatureNotDeletableError(preview.blockers)
    keys = _selected_keys(preview, data.delete_jira_keys)
    # Jira must be reachable before touching the DB: nothing is deleted if it is not configured.
    client = jira_client_mod.get_jira_client() if keys else None
    code = feature.code
    db.add(_audit(actor.id, "feature", feature.id, {"code": code, "jira_keys": keys}))
    await db.execute(delete(TlFeature).where(TlFeature.id == feature_id))
    await db.commit()
    results = await _delete_jira(client, keys) if client else []
    logger.info(
        "Feature %s eliminata da %s; Jira eliminate: %s, fallite: %s",
        log_safe(code),
        log_safe(actor.email),
        log_safe([r.key for r in results if r.status == "deleted"]),
        log_safe([r.key for r in results if r.status == "failed"]),
    )
    return TlDeleteResult(deleted_features=1, jira_results=results)


async def delete_scope(
    db: AsyncSession, scope_id: uuid.UUID, data: TlDeleteRequest
) -> TlDeleteResult:
    actor = await resolve_actor(db, data.actor_email)
    scope = (
        await db.execute(
            select(TlScope)
            .where(TlScope.id == scope_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if scope is None:
        raise ScopeNotFoundError(str(scope_id))
    features = await _scope_features(db, scope_id, lock=True)
    preview = await _build_preview(db, features, scope.jira_qa_epic_key)
    if preview.blockers:
        raise ScopeNotDeletableError(preview.blockers)
    keys = _selected_keys(preview, data.delete_jira_keys)
    client = jira_client_mod.get_jira_client() if keys else None
    code = scope.code
    db.add(
        _audit(
            actor.id,
            "scope",
            scope.id,
            {"code": code, "features": [f.code for f in features], "jira_keys": keys},
        )
    )
    await db.execute(delete(TlScope).where(TlScope.id == scope_id))
    await db.commit()
    results = await _delete_jira(client, keys) if client else []
    logger.info(
        "Ambito %s eliminato da %s con %d feature; Jira eliminate: %s, fallite: %s",
        log_safe(code),
        log_safe(actor.email),
        len(features),
        log_safe([r.key for r in results if r.status == "deleted"]),
        log_safe([r.key for r in results if r.status == "failed"]),
    )
    return TlDeleteResult(deleted_features=len(features), jira_results=results)

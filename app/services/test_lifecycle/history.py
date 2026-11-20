"""Storico della Feature: eventi (audit_logs) e versioni (snapshot di approvazione)."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureVersion,
    TlScenario,
    TlScenarioContent,
    TlScope,
)
from app.models.user import User
from app.schemas.test_lifecycle import TlHistoryItem, TlVersionOut
from app.services.test_lifecycle.gherkin import (
    RenderFeature,
    RenderScenario,
    feature_tag,
    render_feature_file,
)

RESOURCE_TYPE = "tl_feature"
# Attore delle azioni di sistema (sync da Git): nessun utente, `actor_id` NULL negli eventi.
SYSTEM_ACTOR = "qa-hub-sync"


class VersionNotFoundError(Exception):
    pass


def record_event(
    db: AsyncSession,
    feature_id: uuid.UUID,
    action: str,
    actor: User | None,
    detail: dict[str, Any],
) -> None:
    """Scrive un evento di storico in `audit_logs` (unico punto di scrittura del modulo).

    `actor=None`: azione di sistema (sync da Git), registrata senza utente.
    """
    db.add(
        AuditLog(
            actor_id=actor.id if actor is not None else None,
            action=action,
            resource_type=RESOURCE_TYPE,
            resource_id=str(feature_id),
            detail=json.dumps(detail, default=str),
        )
    )


async def build_render(
    db: AsyncSession, feature: TlFeature, revision: TlFeatureRevision
) -> tuple[RenderFeature, list[tuple[TlScenario, TlScenarioContent]]]:
    """Costruisce il modello di rendering della revisione (e le righe scenario/contenuto)."""
    scope_code = (
        await db.execute(select(TlScope.code).where(TlScope.id == feature.scope_id))
    ).scalar_one()
    rows = [
        (s, c)
        for s, c in (
            await db.execute(
                select(TlScenario, TlScenarioContent)
                .join(TlScenarioContent, TlScenarioContent.scenario_id == TlScenario.id)
                .where(TlScenarioContent.revision_id == revision.id)
                .order_by(TlScenarioContent.position)
            )
        ).all()
    ]
    render = RenderFeature(
        code=feature.code,
        feature_tag=feature_tag(scope_code, feature.number),
        title=revision.title,
        description=revision.description,
        background_steps=revision.background_steps,
        scenarios=[
            RenderScenario(
                tag=s.tag,
                title=c.title,
                kind=c.kind,
                polarity=c.polarity,
                extra_tags=list(c.extra_tags or []),
                steps=c.steps,
                examples=c.examples,
                detail_link=c.detail_link,
            )
            for s, c in rows
        ],
    )
    return render, rows


async def snapshot(
    db: AsyncSession,
    feature: TlFeature,
    revision: TlFeatureRevision,
    actor: User | None,
    origin: str = "hub",
    commit_sha: str | None = None,
) -> TlFeatureVersion:
    """Crea la versione: metadati della revisione, scenari (tag + contenuto) e file generato."""
    render, rows = await build_render(db, feature, revision)
    data: dict[str, Any] = {
        "feature_code": feature.code,
        "revision": {
            "id": str(revision.id),
            "number": revision.number,
            "title": revision.title,
            "description": revision.description,
            "background_steps": revision.background_steps,
            "requirements_url": revision.requirements_url,
            "product_issue_key": revision.product_issue_key,
            "reverse_engineering": revision.reverse_engineering,
            "design_estimate_days": (
                str(revision.design_estimate_days)
                if revision.design_estimate_days is not None
                else None
            ),
            "impl_estimate_days": (
                str(revision.impl_estimate_days)
                if revision.impl_estimate_days is not None
                else None
            ),
            "approved_by": revision.approved_by,
            "approved_at": revision.approved_at.isoformat() if revision.approved_at else None,
        },
        "scenarios": [
            {
                "tag": s.tag,
                "position": c.position,
                "title": c.title,
                "kind": c.kind.value,
                "polarity": c.polarity.value,
                "extra_tags": list(c.extra_tags or []),
                "steps": c.steps,
                "examples": c.examples,
                "detail_link": c.detail_link,
            }
            for s, c in rows
        ],
        "file": render_feature_file(render),
    }
    version = TlFeatureVersion(
        feature_id=feature.id,
        revision_id=revision.id,
        origin=origin,
        snapshot=data,
        commit_sha=commit_sha,
        actor=actor.email if actor is not None else SYSTEM_ACTOR,
    )
    db.add(version)
    await db.flush()
    return version


async def list_history(db: AsyncSession, feature_id: uuid.UUID) -> list[TlHistoryItem]:
    """Eventi e versioni, dal più recente. A parità di data: versione prima dell'evento
    (cronologicamente la versione segue l'evento che la produce), poi per id."""
    events = (
        await db.execute(
            select(AuditLog, User.email)
            .outerjoin(User, User.id == AuditLog.actor_id)
            .where(
                AuditLog.resource_type == RESOURCE_TYPE,
                AuditLog.resource_id == str(feature_id),
            )
        )
    ).all()
    versions = (
        await db.execute(select(TlFeatureVersion).where(TlFeatureVersion.feature_id == feature_id))
    ).scalars().all()
    items: list[tuple[Any, int, str, TlHistoryItem]] = []
    for log, email in events:
        try:
            detail = json.loads(log.detail) if log.detail else None
        except ValueError:
            detail = None
        items.append(
            (
                log.created_at,
                0,
                str(log.id),
                TlHistoryItem(
                    kind="event",
                    at=log.created_at,
                    actor=email,
                    action=log.action,
                    detail=detail,
                ),
            )
        )
    for v in versions:
        items.append(
            (
                v.created_at,
                1,
                str(v.id),
                TlHistoryItem(
                    kind="version",
                    at=v.created_at,
                    actor=v.actor,
                    detail={
                        "origin": v.origin,
                        "revision_id": str(v.revision_id),
                        "commit_sha": v.commit_sha,
                    },
                    version_id=v.id,
                ),
            )
        )
    items.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    return [t[3] for t in items]


async def get_version(
    db: AsyncSession, feature_id: uuid.UUID, version_id: uuid.UUID
) -> TlVersionOut:
    v = (
        await db.execute(
            select(TlFeatureVersion).where(
                TlFeatureVersion.id == version_id, TlFeatureVersion.feature_id == feature_id
            )
        )
    ).scalar_one_or_none()
    if v is None:
        raise VersionNotFoundError(str(version_id))
    return TlVersionOut(
        id=v.id,
        feature_id=v.feature_id,
        revision_id=v.revision_id,
        origin=v.origin,
        snapshot=v.snapshot,
        commit_sha=v.commit_sha,
        actor=v.actor,
        created_at=v.created_at,
    )

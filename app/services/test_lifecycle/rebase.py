"""Riapertura di una Feature pubblicata e riallineamento a tre vie (spec §6–§7, fase 2).

Le tre vie: **base** (la revisione su cui la bozza è nata), **git** (la revisione pubblicata,
cioè ciò che è su main) e **draft** (la revisione di lavoro). Un elemento è in conflitto
quando sia Git sia la bozza lo hanno cambiato rispetto alla base, in modo diverso. Gli
elementi non in conflitto prendono automaticamente il lato che è cambiato.

Ordine dei lock: feature → revisione (come nel resto del modulo).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlRevisionStatus,
    TlScenario,
    TlScenarioContent,
)
from app.models.user import User
from app.schemas.test_lifecycle import (
    TlFeatureDetail,
    TlRebaseApply,
    TlRebaseField,
    TlRebaseScenario,
    TlRebaseView,
    TlScenarioContentOut,
)
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import history
from app.services.test_lifecycle.actors import resolve_actor

REOPEN_PHASE1_REASON = "Disponibile dopo il merge del feature file (fase Git)"
REOPEN_OPEN_REVISION_REASON = "Esiste già una revisione in corso"
STALE_REASON = "La bozza è basata su una versione superata: riallineala"

_CONTENT_FIELDS = ("title", "kind", "polarity", "extra_tags", "steps", "examples", "detail_link")
# chiave del confronto -> attributo della revisione
_META_FIELDS = {"title": "title", "description": "description", "background": "background_steps"}
_COPIED_REVISION_FIELDS = (
    "title",
    "description",
    "background_steps",
    "requirements_url",
    "product_issue_key",
    "reverse_engineering",
    "design_estimate_days",
    "impl_estimate_days",
)


class RebaseRequiredError(Exception):
    def __init__(self, conflicts: list[str]):
        super().__init__(", ".join(conflicts))
        self.conflicts = conflicts


class RebaseNotNeededError(Exception):
    pass


# ── riapertura ────────────────────────────────────────────────────────────────


def reopen_reasons(feature: TlFeature) -> list[str]:
    if feature.published_revision_id is None:
        return [REOPEN_PHASE1_REASON]
    if feature.working_revision_id is not None:
        return [REOPEN_OPEN_REVISION_REASON]
    return []


async def reopen(db: AsyncSession, feature: TlFeature, actor: User) -> TlFeatureRevision:
    """Nuova revisione `draft` copiata dalla pubblicata. Il chiamante tiene il lock sulla
    feature, ha verificato `reopen_reasons` e committa."""
    published = await db.get(TlFeatureRevision, feature.published_revision_id)
    assert published is not None  # noqa: S101 - garantito da reopen_reasons
    number = (
        await db.execute(
            select(func.coalesce(func.max(TlFeatureRevision.number), 0)).where(
                TlFeatureRevision.feature_id == feature.id
            )
        )
    ).scalar_one() + 1
    rev = TlFeatureRevision(
        feature_id=feature.id,
        number=number,
        status=TlRevisionStatus.DRAFT,
        base_revision_id=published.id,
        created_by=actor.email,
        **{f: getattr(published, f) for f in _COPIED_REVISION_FIELDS},
    )
    db.add(rev)
    await db.flush()
    for content in await _contents(db, published.id):
        db.add(
            TlScenarioContent(
                revision_id=rev.id,
                scenario_id=content.scenario_id,
                position=content.position,
                **{f: getattr(content, f) for f in _CONTENT_FIELDS},
            )
        )
    feature.working_revision_id = rev.id
    history.record_event(
        db, feature.id, "tl.revision.reopened", actor,
        {
            "revision_id": str(rev.id),
            "revision_number": rev.number,
            "base_revision_id": str(published.id),
        },
    )
    await db.flush()
    return rev


# ── confronto ─────────────────────────────────────────────────────────────────


async def _contents(db: AsyncSession, revision_id: uuid.UUID | None) -> list[TlScenarioContent]:
    if revision_id is None:
        return []
    return list(
        (
            await db.execute(
                select(TlScenarioContent)
                .where(TlScenarioContent.revision_id == revision_id)
                .order_by(TlScenarioContent.position)
            )
        ).scalars()
    )


def _key(content: TlScenarioContent | None) -> tuple[Any, ...] | None:
    if content is None:
        return None
    values = (getattr(content, f) for f in _CONTENT_FIELDS)
    return tuple(v.value if hasattr(v, "value") else v for v in values)


def _out(content: TlScenarioContent | None) -> TlScenarioContentOut | None:
    if content is None:
        return None
    return TlScenarioContentOut(
        position=content.position, **{f: getattr(content, f) for f in _CONTENT_FIELDS}
    )


def _is_conflict(base: Any, git: Any, draft: Any) -> bool:
    return git != base and draft != base and git != draft


class _Three:
    """Stato a tre vie di una revisione di lavoro."""

    def __init__(
        self,
        rev: TlFeatureRevision,
        base_rev: TlFeatureRevision | None,
        pub_rev: TlFeatureRevision,
        base: dict[uuid.UUID, TlScenarioContent],
        git: dict[uuid.UUID, TlScenarioContent],
        draft: dict[uuid.UUID, TlScenarioContent],
        tags: dict[uuid.UUID, str],
    ) -> None:
        self.rev, self.base_rev, self.pub_rev = rev, base_rev, pub_rev
        self.base, self.git, self.draft, self.tags = base, git, draft, tags

    def scenario_ids(self) -> list[uuid.UUID]:
        ids = list(dict.fromkeys([*self.git, *self.draft, *self.base]))
        return sorted(ids, key=lambda i: self.tags.get(i, ""))

    def meta(self, attr: str) -> tuple[str | None, str | None, str | None]:
        base = getattr(self.base_rev, attr) if self.base_rev is not None else None
        return base, getattr(self.pub_rev, attr), getattr(self.rev, attr)

    def conflicts(self) -> list[str]:
        out = [
            key for key, attr in _META_FIELDS.items() if _is_conflict(*self.meta(attr))
        ]
        for sid in self.scenario_ids():
            if _is_conflict(
                _key(self.base.get(sid)), _key(self.git.get(sid)), _key(self.draft.get(sid))
            ):
                out.append(str(sid))
        return out


async def _load(db: AsyncSession, feature: TlFeature, rev: TlFeatureRevision) -> _Three:
    if feature.published_revision_id is None:
        raise RebaseNotNeededError(str(rev.id))
    pub_rev = await db.get(TlFeatureRevision, feature.published_revision_id)
    assert pub_rev is not None  # noqa: S101 - FK
    base_rev = (
        await db.get(TlFeatureRevision, rev.base_revision_id) if rev.base_revision_id else None
    )
    base = {c.scenario_id: c for c in await _contents(db, rev.base_revision_id)}
    git = {c.scenario_id: c for c in await _contents(db, pub_rev.id)}
    draft = {c.scenario_id: c for c in await _contents(db, rev.id)}
    tags = {
        s.id: s.tag
        for s in (
            await db.execute(select(TlScenario).where(TlScenario.feature_id == feature.id))
        ).scalars()
    }
    return _Three(rev, base_rev, pub_rev, base, git, draft, tags)


async def get_rebase_view(
    db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID
) -> TlRebaseView:
    feature = await db.get(TlFeature, feature_id)
    if feature is None:
        raise features_svc.FeatureNotFoundError(str(feature_id))
    rev = await db.get(TlFeatureRevision, revision_id)
    if rev is None or rev.feature_id != feature_id:
        raise features_svc.RevisionNotFoundError(str(revision_id))
    three = await _load(db, feature, rev)
    metadata = []
    for key, attr in _META_FIELDS.items():
        base, git, draft = three.meta(attr)
        metadata.append(
            TlRebaseField(
                field=key, base=base, git=git, draft=draft,
                conflict=_is_conflict(base, git, draft),
            )
        )
    scenarios = []
    for sid in three.scenario_ids():
        b, g, d = three.base.get(sid), three.git.get(sid), three.draft.get(sid)
        scenarios.append(
            TlRebaseScenario(
                scenario_id=sid,
                tag=three.tags.get(sid, ""),
                base=_out(b),
                git=_out(g),
                draft=_out(d),
                conflict=_is_conflict(_key(b), _key(g), _key(d)),
            )
        )
    view = TlRebaseView(
        revision_id=rev.id,
        base_revision_id=rev.base_revision_id,
        published_revision_id=feature.published_revision_id,
        lock_version=rev.lock_version,
        metadata=metadata,
        scenarios=scenarios,
    )
    await db.rollback()  # sola lettura: chiude la transazione
    return view


# ── applicazione ──────────────────────────────────────────────────────────────


def _take_git(base: Any, git: Any, draft: Any, choice: str | None) -> bool:
    """True se per questo elemento va preso il lato Git."""
    if _is_conflict(base, git, draft):
        return choice == "git"
    return git != base and draft == base


async def apply_rebase(
    db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID, data: TlRebaseApply
) -> TlFeatureDetail:
    actor = await resolve_actor(db, data.actor_email)
    feature = await features_svc._lock_feature(db, feature_id)
    rev = await features_svc._lock_revision(db, feature_id, revision_id)
    features_svc._require_draft(rev)
    features_svc._require_lock(rev, data.lock_version)
    if not rev.base_stale or feature.working_revision_id != rev.id:
        raise RebaseNotNeededError(str(rev.id))
    three = await _load(db, feature, rev)
    missing = [k for k in three.conflicts() if k not in data.choices]
    if missing:
        raise RebaseRequiredError(missing)

    taken: list[str] = []
    for key, attr in _META_FIELDS.items():
        base, git, draft = three.meta(attr)
        if _take_git(base, git, draft, data.choices.get(key)):
            setattr(rev, attr, git if attr != "title" else (git or rev.title))
            taken.append(key)

    next_position = max((c.position for c in three.draft.values()), default=0)
    for sid in three.scenario_ids():
        b, g, d = three.base.get(sid), three.git.get(sid), three.draft.get(sid)
        if not _take_git(_key(b), _key(g), _key(d), data.choices.get(str(sid))):
            continue
        taken.append(str(sid))
        if g is None:
            if d is not None:
                await db.delete(d)
            continue
        if d is None:
            next_position += 1
            d = TlScenarioContent(revision_id=rev.id, scenario_id=sid, position=next_position)
            db.add(d)
        for f in _CONTENT_FIELDS:
            setattr(d, f, getattr(g, f))
    await db.flush()
    await features_svc._compact_positions(db, rev.id)

    rev.base_revision_id = three.pub_rev.id
    rev.base_stale = False
    rev.lock_version += 1
    history.record_event(
        db, feature.id, "tl.revision.rebased", actor,
        {
            "revision_id": str(rev.id),
            "revision_number": rev.number,
            "base_revision_id": str(three.pub_rev.id),
            "choices": dict(data.choices),
            "taken_from_git": taken,
        },
    )
    await db.commit()
    await db.refresh(rev)  # updated_at (onupdate) è scaduto dopo il commit
    return await features_svc.get_feature_detail(db, feature_id, actor.email)

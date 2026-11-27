"""Adapting a feature to the scope it lives in: code, number, scenario tags and aliases.

The scope code is part of the feature code (`FEAT_004_<scope>`) and of the scenario identity
tags (`@<SCOPE>_004_01`). Moving a feature to another scope, or renaming the code of its scope,
gives it the identifiers of the new scope; the previous scenario tags stay as aliases so the NRT
executions keep their link. This is only safe while the repository does not carry the hub
identifiers of the feature (see `adapt_blockers`). The caller holds the locks and commits.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlMigrationItem,
    TlRevisionStatus,
    TlScenario,
    TlScenarioAlias,
    TlScope,
)
from app.models.user import User
from app.schemas.test_lifecycle import TlMoveItem
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import history
from app.services.test_lifecycle.advisory_lock import try_advisory_lock
from app.services.test_lifecycle.alias_guard import number_held_by_aliases
from app.services.test_lifecycle.branch_sync_rules import default_branch_name
from app.services.test_lifecycle.gherkin import feature_code, scenario_tag

_PR_REASON = "Ha una PR aperta: i suoi file portano già i codici e i tag attuali"
_MAIN_REASON = (
    "Il file è già su main con i codici e i tag dell'hub: serve una PR per cambiarli "
    "(si può spostare senza adeguare)"
)
_BRANCH_REASON = (
    "Il branch di lavoro è già stato scritto sul repository con i tag attuali: "
    "si può spostare senza adeguare"
)


class AdaptBlockedError(Exception):
    """Some features cannot be adapted. `blocked`: `{feature_id, code, reasons}` for each."""

    def __init__(self, blocked: list[dict]) -> None:
        super().__init__(", ".join(b["code"] for b in blocked))
        self.blocked = blocked


async def adapt_blockers(db: AsyncSession, feature: TlFeature) -> list[str]:
    """Why `feature` cannot be adapted (empty when it can): its ids are on the repository."""
    revisions = list(
        (
            await db.execute(
                select(TlFeatureRevision).where(TlFeatureRevision.feature_id == feature.id)
            )
        ).scalars()
    )
    reasons: list[str] = []
    if any(r.pr_state == "open" for r in revisions):
        reasons.append(_PR_REASON)
    if feature.published_revision_id is not None and not feature.alignment_pending:
        reasons.append(_MAIN_REASON)
    open_states = (TlRevisionStatus.MERGED, TlRevisionStatus.SUPERSEDED)
    if any(r.work_hub_sha is not None and r.status not in open_states for r in revisions):
        reasons.append(_BRANCH_REASON)
    return reasons


async def adapt_feature(
    db: AsyncSession, feature: TlFeature, scope: TlScope, number: int
) -> tuple[str, str]:
    """Gives `feature` the identifiers of `scope` with `number`. Returns `(old code, new code)`.

    Raises `AdaptBlockedError` (changing nothing) when the feature cannot be adapted.
    """
    reasons = await adapt_blockers(db, feature)
    if reasons:
        raise AdaptBlockedError(
            [{"feature_id": feature.id, "code": feature.code, "reasons": reasons}]
        )
    old_code = feature.code
    new_code = feature_code(scope.code, number)

    scenarios = list(
        (
            await db.execute(
                select(TlScenario)
                .where(TlScenario.feature_id == feature.id)
                .order_by(TlScenario.number)
            )
        ).scalars()
    )
    known = set((await db.execute(select(TlScenarioAlias.alias))).scalars())
    for sc in scenarios:
        new_tag = scenario_tag(scope.code, number, sc.number)
        if sc.tag == new_tag:
            continue
        alias = sc.tag.lstrip("@")
        if alias not in known:
            db.add(TlScenarioAlias(scenario_id=sc.id, alias=alias))
            known.add(alias)
        sc.tag = new_tag

    revisions = (
        await db.execute(
            select(TlFeatureRevision).where(TlFeatureRevision.feature_id == feature.id)
        )
    ).scalars()
    for rev in revisions:
        if rev.work_branch == default_branch_name(old_code, rev.number):
            rev.work_branch = default_branch_name(new_code, rev.number)

    item = (
        await db.execute(select(TlMigrationItem).where(TlMigrationItem.feature_id == feature.id))
    ).scalar_one_or_none()
    if item is not None:
        item.scope_code = scope.code
        item.number = number
        overrides = dict(item.overrides or {})
        for key, value in (("scope_code", scope.code), ("number", number)):
            if key in overrides:
                overrides[key] = value
        item.overrides = overrides

    feature.scope_id = scope.id
    feature.number = number
    feature.code = new_code
    await db.flush()
    return old_code, new_code


def blocked_of(features: list[tuple[uuid.UUID, str, list[str]]]) -> AdaptBlockedError:
    return AdaptBlockedError(
        [{"feature_id": fid, "code": code, "reasons": reasons} for fid, code, reasons in features]
    )


class MigrationRunningError(Exception):
    """A Confluence migration is running: scopes and features are not moved meanwhile."""


async def move_features(
    db: AsyncSession,
    feature_ids: list[uuid.UUID],
    target_scope_id: uuid.UUID,
    actor: User,
) -> tuple[list[TlMoveItem], list[uuid.UUID]]:
    """Moves features to the target scope, all or nothing. Returns `(moved, skipped ids)`.

    Each feature takes the code, number and scenario tags of the target scope; the request fails
    with `AdaptBlockedError`, changing nothing, if any cannot be adapted (a move that kept the
    old identifiers would leave file header, feature tag and scenario tags out of step).
    Features already in the target are skipped.
    """
    from app.services.test_lifecycle.migration import LOCK_KEY
    from app.services.test_lifecycle.scopes import ScopeNotFoundError

    ids = list(dict.fromkeys(feature_ids))
    async with try_advisory_lock(db, LOCK_KEY) as got:
        if not got:
            raise MigrationRunningError
        target = (
            await db.execute(
                select(TlScope)
                .where(TlScope.id == target_scope_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if target is None:
            raise ScopeNotFoundError(str(target_scope_id))
        features = list(
            (
                await db.execute(
                    select(TlFeature)
                    .where(TlFeature.id.in_(ids))
                    .order_by(TlFeature.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        if len(features) != len(ids):
            raise features_svc.FeatureNotFoundError(str(ids))
        by_id = {f.id: f for f in features}
        ordered = [by_id[i] for i in ids]
        skipped = [f.id for f in ordered if f.scope_id == target.id]
        to_move = [f for f in ordered if f.scope_id != target.id]

        blocked = []
        for f in to_move:
            reasons = await adapt_blockers(db, f)
            if reasons:
                blocked.append((f.id, f.code, reasons))
        if blocked:
            await db.rollback()
            raise blocked_of(blocked)

        moved: list[TlMoveItem] = []
        for f in to_move:
            old_scope = await db.get(TlScope, f.scope_id)
            assert old_scope is not None  # noqa: S101 - FK guarantees it
            old_scope_code, old_code = old_scope.code, f.code
            number = target.next_feature_number
            while (
                await db.execute(
                    select(TlFeature.id)
                    .where(TlFeature.code == feature_code(target.code, number))
                    .limit(1)
                )
            ).first() is not None or await number_held_by_aliases(db, target.code, number):
                number += 1
            target.next_feature_number = number + 1
            _, new_code = await adapt_feature(db, f, target, number)
            history.record_event(
                db,
                f.id,
                "tl.feature.moved",
                actor,
                {
                    "old_scope": old_scope_code,
                    "new_scope": target.code,
                    "old_code": old_code,
                    "new_code": new_code,
                    "adapted": True,
                },
            )
            moved.append(
                TlMoveItem(
                    feature_id=f.id,
                    old_code=old_code,
                    new_code=new_code,
                    old_scope_code=old_scope_code,
                    new_scope_code=target.code,
                )
            )
        await db.commit()
    return moved, skipped

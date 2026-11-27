"""Recovery: a feature emptied by a legacy file on main is rebuilt from that file.

When a file without the hub header (a legacy version put back on main by an unrelated merge) was
imported as a new Git revision with no valid scenario, the feature was left empty although its
last good revision still holds the scenarios. Here main wins: the content of that revision is
rewritten from the file on main, matching scenarios by position, the feature goes back to waiting
for its alignment pull request and the emptied revision and the empty draft are put away.
A person decides the cases that are not clear-cut (different number of scenarios, a draft with
work in it): they are reported and left untouched.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlIssueKind,
    TlMigrationItem,
    TlRevisionStatus,
    TlScenario,
    TlScenarioAlias,
    TlScenarioContent,
    TlScope,
)
from app.services.test_lifecycle import history
from app.services.test_lifecycle import issues as issues_svc
from app.services.test_lifecycle.category_tags import known_tags
from app.services.test_lifecycle.feature_parser import parse_feature_file
from app.services.test_lifecycle.migration import _feature_tags, _scenarios_from_file


@dataclass
class RealignReport:
    code: str
    status: str  # "ready" (dry run), "applied" or "skipped"
    reason: str = ""
    scenarios: int = 0
    baseline_revision: int | None = None
    changed_scenarios: int = 0


async def _count(db: AsyncSession, model: Any, *where: Any) -> int:
    return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def realign_from_main(
    db: AsyncSession, git: Any, feature_id: uuid.UUID, *, apply: bool
) -> RealignReport:
    """Rebuild the feature from its file on main (`apply=False`: only report what would change)."""
    feature = await db.get(TlFeature, feature_id)
    if feature is None:
        raise LookupError(str(feature_id))
    code = feature.code

    def skip(reason: str) -> RealignReport:
        return RealignReport(code, "skipped", reason)

    if not feature.git_path or feature.published_revision_id is None:
        return skip("la feature non ha un file o una revisione pubblicata")
    published = await db.get(TlFeatureRevision, feature.published_revision_id)
    assert published is not None  # noqa: S101 - FK
    if await _count(db, TlScenarioContent, TlScenarioContent.revision_id == published.id):
        return skip("la pubblicata ha già degli scenari: nulla da recuperare")
    baseline = (
        await db.execute(
            select(TlFeatureRevision)
            .where(
                TlFeatureRevision.feature_id == feature.id,
                TlFeatureRevision.number < published.number,
                select(func.count())
                .select_from(TlScenarioContent)
                .where(TlScenarioContent.revision_id == TlFeatureRevision.id)
                .scalar_subquery()
                > 0,
            )
            .order_by(TlFeatureRevision.number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if baseline is None:
        return skip("nessuna revisione precedente con scenari")
    working = (
        await db.get(TlFeatureRevision, feature.working_revision_id)
        if feature.working_revision_id
        else None
    )
    if working is not None and (
        await _count(db, TlScenarioContent, TlScenarioContent.revision_id == working.id)
        or working.pr_number is not None
    ):
        return skip("la bozza di lavoro ha contenuto o una PR: va gestita a mano")

    head = await git.head_sha(settings.tl_base_branch)
    file = await git.get_file(feature.git_path, head)
    if file is None:
        return skip(f"file {feature.git_path} non trovato su main")
    parsed = parse_feature_file(file.text)
    if parsed.errors:
        return skip("il file su main non è leggibile: " + "; ".join(parsed.errors)[:200])
    scope = await db.get(TlScope, feature.scope_id)
    assert scope is not None  # noqa: S101 - FK
    from app.models.test_lifecycle import TlMigrationTag

    keep = {
        t.name
        for t in (await db.execute(select(TlMigrationTag))).scalars()
        if t.decision == "keep"
    } | await known_tags(db)
    specs = _scenarios_from_file(
        parsed, _feature_tags(file.text), keep, scope.code, feature.number
    )
    rows = list(
        (
            await db.execute(
                select(TlScenarioContent, TlScenario)
                .join(TlScenario, TlScenario.id == TlScenarioContent.scenario_id)
                .where(TlScenarioContent.revision_id == baseline.id)
                .order_by(TlScenarioContent.position)
            )
        ).all()
    )
    if len(rows) != len(specs):
        return skip(
            f"il file su main ha {len(specs)} scenari, l'hub {len(rows)}: da confrontare a mano"
        )
    changed = sum(
        1
        for (c, _s), spec in zip(rows, specs, strict=True)
        if (c.title, c.steps) != (spec["title"].strip()[:500], spec["steps"])
    )
    report = RealignReport(
        code, "ready", scenarios=len(specs), baseline_revision=baseline.number,
        changed_scenarios=changed,
    )
    if not apply:
        await db.rollback()
        return report

    for (c, scenario), spec in zip(rows, specs, strict=True):
        c.title = spec["title"].strip()[:500]
        c.kind = spec["kind"]
        c.polarity = spec["polarity"]
        c.extra_tags = list(spec["extra_tags"])
        c.steps = spec["steps"]
        c.examples = spec["examples"]
        c.detail_link = spec["detail_link"]
        old = spec["old"]
        if old and old != scenario.tag.lstrip("@"):
            taken = await _count(db, TlScenarioAlias, TlScenarioAlias.alias == old)
            if not taken:
                db.add(TlScenarioAlias(scenario_id=scenario.id, alias=old))
    if parsed.title.strip():
        baseline.title = parsed.title.strip()[:255]
    baseline.description = parsed.description
    baseline.background_steps = parsed.background_steps
    baseline.lock_version += 1
    baseline.status = TlRevisionStatus.MERGED
    published.status = TlRevisionStatus.SUPERSEDED
    feature.working_revision_id = None
    feature.published_revision_id = baseline.id
    feature.alignment_pending = True
    await db.flush()
    if working is not None:
        await db.execute(delete(TlFeatureRevision).where(TlFeatureRevision.id == working.id))
    await issues_svc.resolve_feature_issues(
        db, kind=TlIssueKind.STRUCTURAL_CHANGE, feature_id=feature.id
    )
    item = (
        await db.execute(select(TlMigrationItem).where(TlMigrationItem.feature_id == feature.id))
    ).scalar_one_or_none()
    if item is not None:
        item.proposal = {**item.proposal, "old_scenario_tags": [s["old"] for s in specs]}
    await history.snapshot(db, feature, baseline, None, origin="git", commit_sha=head)
    history.record_event(
        db, feature.id, "tl.feature.realigned_from_main", None,
        {
            "baseline_revision": baseline.number,
            "emptied_revision": published.number,
            "commit_sha": head,
            "at": datetime.now(UTC).isoformat(),
        },
    )
    await db.commit()
    report.status = "applied"
    return report

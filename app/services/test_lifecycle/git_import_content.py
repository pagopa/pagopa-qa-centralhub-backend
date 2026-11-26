"""Contenuto importato da Git: scenari validi, identità nuove e righe della revisione."""

from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlPolarity,
    TlRevisionStatus,
    TlScenario,
    TlScenarioContent,
    TlScenarioKind,
)
from app.services.test_lifecycle.feature_parser import ParsedFeature, ParsedScenario
from app.services.test_lifecycle.gherkin import RenderFeature, feature_tag
from app.services.test_lifecycle.history import SYSTEM_ACTOR

_TITLE_MAX = 255
_SCENARIO_TITLE_MAX = 500
_LINK_MAX = 1024


def empty_render(feature: TlFeature, scope_code: str) -> RenderFeature:
    """Lato atteso quando la feature non ha ancora una revisione pubblicata."""
    return RenderFeature(
        code=feature.code,
        feature_tag=feature_tag(scope_code, feature.number),
        title="",
        description=None,
        background_steps=None,
        scenarios=[],
    )


def fill_defaults(parsed: ParsedFeature, expected: RenderFeature) -> None:
    """Valori assenti su Git = invariati: polarità senza tag e titolo vuoto.

    Senza questo un file privo di `@positivo/@negativo` produrrebbe una differenza (e una
    versione) a ogni import, perché il render dell'hub scrive sempre la polarità.
    """
    known = {sc.tag: sc for sc in expected.scenarios}
    for sc in parsed.scenarios:
        if sc.polarity is None:
            prev = known.get(sc.identity_tag or "")
            sc.polarity = prev.polarity if prev is not None else TlPolarity.POSITIVO
    if not parsed.title.strip():
        parsed.title = expected.title


def valid_scenarios(
    parsed: ParsedFeature, scope_code: str, feature_number: int
) -> list[ParsedScenario]:
    """Scenari importabili, nell'ordine del file: tag identità di questa feature, prima occorrenza.

    Stesse regole di `classify`: gli scenari senza tag, con tag estraneo o duplicato sono
    segnalati come anomalia strutturale e restano fuori dalla revisione.
    """
    prefix = re.compile(rf"^@{re.escape(scope_code.upper())}_{feature_number:03d}_\d{{2}}$")
    seen: set[str] = set()
    out: list[ParsedScenario] = []
    for sc in parsed.scenarios:
        tag = sc.identity_tag
        if tag is None or not prefix.match(tag) or tag in seen:
            continue
        seen.add(tag)
        out.append(sc)
    return out


async def new_git_revision(
    db: AsyncSession,
    feature: TlFeature,
    published: TlFeatureRevision | None,
    commit_sha: str,
    title: str,
) -> TlFeatureRevision:
    """Revisione `origin="git"`, già `merged`, con i metadati di progettazione della pubblicata."""
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
        status=TlRevisionStatus.MERGED,
        base_revision_id=published.id if published is not None else None,
        title=(title or (published.title if published else "") or feature.code)[:_TITLE_MAX],
        created_by=SYSTEM_ACTOR,
        origin="git",
        merged_commit_sha=commit_sha,
    )
    if published is not None:
        rev.requirements_url = published.requirements_url
        rev.product_issue_key = published.product_issue_key
        rev.reverse_engineering = published.reverse_engineering
        rev.design_estimate_days = published.design_estimate_days
        rev.impl_estimate_days = published.impl_estimate_days
        # a Git-side change has no Jira task of its own: it inherits the published revision's keys
        # (never the feature's, which mirror an open draft when there is one)
        rev.jira_design_key = published.jira_design_key
        rev.jira_impl_key = published.jira_impl_key
        rev.jira_qa_epic_key = published.jira_qa_epic_key
    db.add(rev)
    await db.flush()
    return rev


async def apply_content(
    db: AsyncSession,
    feature: TlFeature,
    target: TlFeatureRevision,
    parsed: ParsedFeature,
    valid: list[ParsedScenario],
) -> list[TlScenario]:
    """Scrive nella revisione il contenuto di Git. Ritorna le identità create.

    Le identità nuove usano il numero del tag; `next_scenario_number` va oltre il massimo.
    Le righe di contenuto sono aggiornate sul posto (vincolo unico revisione+scenario).
    """
    if parsed.title.strip():
        target.title = parsed.title.strip()[:_TITLE_MAX]
    target.description = parsed.description
    target.background_steps = parsed.background_steps

    identities = {
        s.tag: s
        for s in (
            await db.execute(select(TlScenario).where(TlScenario.feature_id == feature.id))
        ).scalars()
    }
    created: list[TlScenario] = []
    for sc in valid:
        tag = sc.identity_tag
        assert tag is not None  # noqa: S101 - filtrato da valid_scenarios
        if tag in identities:
            continue
        number = int(tag.rsplit("_", 1)[1])
        scenario = TlScenario(feature_id=feature.id, number=number, tag=tag)
        db.add(scenario)
        identities[tag] = scenario
        created.append(scenario)
        feature.next_scenario_number = max(feature.next_scenario_number, number + 1)
    await db.flush()

    contents = {
        c.scenario_id: c
        for c in (
            await db.execute(
                select(TlScenarioContent).where(TlScenarioContent.revision_id == target.id)
            )
        ).scalars()
    }
    keep: set[uuid.UUID] = set()
    for position, sc in enumerate(valid, start=1):
        scenario = identities[sc.identity_tag or ""]
        keep.add(scenario.id)
        content = contents.get(scenario.id)
        if content is None:
            content = TlScenarioContent(revision_id=target.id, scenario_id=scenario.id)
            db.add(content)
        content.position = position
        content.title = sc.title.strip()[:_SCENARIO_TITLE_MAX]
        content.kind = sc.kind
        content.polarity = sc.polarity or TlPolarity.POSITIVO
        content.extra_tags = list(sc.extra_tags)
        content.steps = sc.steps
        content.examples = sc.examples if sc.kind == TlScenarioKind.OUTLINE else None
        content.detail_link = sc.detail_link[:_LINK_MAX] if sc.detail_link else None
    for scenario_id, content in contents.items():
        if scenario_id not in keep:
            await db.delete(content)
    target.lock_version += 1
    await db.flush()
    return created

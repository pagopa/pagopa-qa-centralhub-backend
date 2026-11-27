"""Adoption of a feature file the hub does not manage (an `unmanaged_file` or an
`unknown_feature_code` question).

The file on `main` becomes a published feature of the hub. A file that already carries a valid
`# qa-hub:` header keeps its code; one without a header gets the next free code of the scope and
an alignment pull request (the same used by the migration) writes the header and the identity
tags into the file. Nothing is written to Git outside a pull request.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureStatus,
    TlIssue,
    TlIssueKind,
    TlIssueStatus,
    TlRevisionStatus,
    TlScenario,
    TlScenarioAlias,
    TlScenarioContent,
    TlScope,
)
from app.schemas.test_lifecycle import TlAdoptOut
from app.services.test_lifecycle import history, migration_align
from app.services.test_lifecycle import issues as issues_svc
from app.services.test_lifecycle.actors import resolve_actor
from app.services.test_lifecycle.category_tags import known_tags
from app.services.test_lifecycle.feature_parser import normalize_text, parse_feature_file
from app.services.test_lifecycle.gherkin import feature_code, render_feature_file
from app.services.test_lifecycle.migration import (
    _feature_tags,
    _hub_numbers,
    _scenarios_from_file,
    code_parts,
    test_type_of,
)
from app.services.test_lifecycle.tags import default_test_type

ADOPTABLE = (TlIssueKind.UNMANAGED_FILE, TlIssueKind.UNKNOWN_FEATURE_CODE)


class AdoptError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AdoptNotApplicable(AdoptError):  # noqa: N818
    """The issue is not an open file question of this kind."""


class AdoptFileMissing(AdoptError):  # noqa: N818
    """The file is not on `main` any more."""


class AdoptParseError(AdoptError):  # noqa: N818
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class AdoptScopeRequired(AdoptError):  # noqa: N818
    """The folder does not tell the scope: the user must choose it."""


class AdoptConflict(AdoptError):  # noqa: N818
    """The code or the scope cannot be used."""


@dataclass
class _Plan:
    scope: TlScope
    number: int


async def _plan(
    db: AsyncSession, header_code: str | None, requested: str | None, suggested: str | None
) -> _Plan:
    """Scope and number of the new feature (scope row locked)."""
    parts = code_parts(header_code)
    scope_code = requested or (parts[0] if parts else None) or suggested
    if parts and requested and parts[0].lower() != requested.lower():
        raise AdoptConflict(f"L'intestazione del file indica l'ambito {parts[0]}, non {requested}.")
    if not scope_code:
        raise AdoptScopeRequired("Scegli l'ambito della feature: la cartella non lo indica.")
    scope = (
        await db.execute(
            select(TlScope).where(func.lower(TlScope.code) == scope_code.lower()).with_for_update()
        )
    ).scalar_one_or_none()
    if scope is None:
        raise AdoptConflict(f"L'ambito {scope_code} non esiste.")
    taken = await _hub_numbers(db)
    if parts:
        number = parts[1]
        if (scope.code.lower(), number) in taken:
            raise AdoptConflict(f"Il codice {feature_code(scope.code, number)} è già usato.")
    else:
        number = scope.next_feature_number
        while (scope.code.lower(), number) in taken:
            number += 1
    return _Plan(scope, number)


async def _store_scenarios(
    db: AsyncSession,
    feature: TlFeature,
    rev: TlFeatureRevision,
    specs: list[dict[str, Any]],
) -> None:
    aliases = set((await db.execute(select(TlScenarioAlias.alias))).scalars())
    for position, spec in enumerate(specs, start=1):
        scenario = TlScenario(feature_id=feature.id, number=position, tag=spec["tag"])
        db.add(scenario)
        await db.flush()
        db.add(
            TlScenarioContent(
                revision_id=rev.id,
                scenario_id=scenario.id,
                position=position,
                title=spec["title"][:500],
                kind=spec["kind"],
                polarity=spec["polarity"],
                extra_tags=spec["extra_tags"],
                steps=spec["steps"],
                examples=spec["examples"],
                detail_link=spec["detail_link"],
            )
        )
        old = spec["old"]
        if old and old not in aliases and old != spec["tag"].lstrip("@"):
            db.add(TlScenarioAlias(scenario_id=scenario.id, alias=old))
            aliases.add(old)
    feature.next_scenario_number = len(specs) + 1


async def adopt_file(
    db: AsyncSession,
    issue_id: uuid.UUID,
    actor_email: str,
    *,
    git: Any,
    scope_code: str | None = None,
) -> TlAdoptOut:
    """Creates the feature of the issue's file and, if the file needs the hub's header or tags,
    opens the alignment pull request. One commit for the feature, one for the pull request."""
    await resolve_actor(db, actor_email)  # 403 before the network
    issue = await db.get(TlIssue, issue_id)
    if issue is None:
        raise issues_svc.IssueNotFoundError(str(issue_id))
    if (
        issue.kind not in ADOPTABLE
        or issue.status == TlIssueStatus.RESOLVED
        or issue.feature_id is not None
        or not issue.path
    ):
        raise AdoptNotApplicable("Questa anomalia non è un file da importare.")
    path = issue.path
    suggested = (issue.detail or {}).get("scope_code")
    await db.rollback()  # no transaction open while talking to Git

    head = await git.head_sha(settings.tl_base_branch)
    file = await git.get_file(path, head)
    if file is None:
        raise AdoptFileMissing(f"Il file {path} non è più su {settings.tl_base_branch}.")
    text = normalize_text(file.text)
    allowed = await known_tags(db)
    parsed = parse_feature_file(text, allowed_extra=())
    if parsed.errors:
        raise AdoptParseError(parsed.errors)
    if not parsed.scenarios:
        raise AdoptParseError(["Il file non contiene scenari"])

    actor = await resolve_actor(db, actor_email)  # the rollback above expired the first one
    plan = await _plan(db, parsed.code, scope_code, suggested)
    scope, number = plan.scope, plan.number
    scope.next_feature_number = max(scope.next_feature_number, number + 1)
    feature_tags = _feature_tags(text)
    code = feature_code(scope.code, number)

    feature = TlFeature(
        scope_id=scope.id,
        number=number,
        code=code,
        status=TlFeatureStatus.IMPLEMENTED,
        test_type=test_type_of(feature_tags) or default_test_type(scope.repo_folder),
        created_by=actor.email,
        migrated_from={"adopted_from": path, "commit_sha": head},
        git_path=path,
        git_blob_sha=file.blob_sha,
    )
    db.add(feature)
    await db.flush()
    rev = TlFeatureRevision(
        feature_id=feature.id,
        number=1,
        status=TlRevisionStatus.MERGED,
        title=(parsed.title or code)[:255],
        description=parsed.description,
        background_steps=parsed.background_steps,
        created_by=actor.email,
        origin="git",
        merged_commit_sha=head,
    )
    db.add(rev)
    await db.flush()
    specs = _scenarios_from_file(
        parsed, feature_tags, set(allowed), scope.code, number, keep_all=True
    )
    await _store_scenarios(db, feature, rev, specs)
    feature.published_revision_id = rev.id
    await db.flush()

    render, _ = await history.build_render(db, feature, rev)
    pending = normalize_text(render_feature_file(render)) != text
    feature.alignment_pending = pending
    feature.migrated_from = {
        "adopted_from": path,
        "commit_sha": head,
        "old_scenario_tags": [s["old"] for s in specs],
    }
    await history.snapshot(db, feature, rev, actor, origin="git", commit_sha=head)
    history.record_event(
        db,
        feature.id,
        "tl.feature.adopted",
        actor,
        {"path": path, "code": code, "commit_sha": head, "issue_id": str(issue_id)},
    )
    for kind in ADOPTABLE:
        for resolved in await issues_svc.resolve_path_issues(db, kind=kind, path=path):
            resolved.updated_by = actor.email
            resolved.updated_at = datetime.now(UTC)
    feature_id = feature.id
    await db.commit()

    out = TlAdoptOut(feature_id=feature_id, code=code, alignment_pending=pending)
    if pending:
        aligned = await migration_align.open_alignment_pr(db, scope.code, git)
        out.pr_url, out.pr_error = aligned.pr_url, aligned.pr_error
    return out

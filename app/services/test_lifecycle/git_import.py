"""Import dei feature file da Git: pubblicazione al merge, versioni `git` e anomalie (spec §7).

Chiamato dal sync (Task 8), che tiene il lock advisory globale `tl_git_sync`.

- Nessun lock di riga durante le chiamate GitHub: prima si legge il file, poi si bloccano le
  righe e si applica. Ordine dei lock: feature -> revisione pubblicata -> revisione di lavoro
  (o mergiata) -> anomalie.
- Un commit per file: o tutto l'import del file o niente (nessuno stato parziale).
- Le azioni sono di sistema: eventi con `actor_id` NULL, versioni con attore `qa-hub-sync`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlIssueKind,
    TlIssueStatus,
    TlRevisionStatus,
    TlScenario,
    TlScope,
)
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import git_import_content as content
from app.services.test_lifecycle import history
from app.services.test_lifecycle import issues as issues_svc
from app.services.test_lifecycle.feature_parser import (
    DiffResult,
    ParsedFeature,
    ParsedScenario,
    classify,
    normalize_text,
    parse_feature_file,
)
from app.services.test_lifecycle.gherkin import render_feature_file
from app.services.test_lifecycle.git_client import TlGitClient

SYSTEM_ACTOR = history.SYSTEM_ACTOR
STRUCTURAL_KEY = "structural"


@dataclass
class _Fetched:
    path: str
    previous_path: str | None
    commit_sha: str
    text: str  # normalizzato
    blob_sha: str
    parsed: ParsedFeature
    allowed_extra: tuple[str, ...] | None = None  # None: keep every well-formed tag


@dataclass
class _Locked:
    feature: TlFeature
    scope: TlScope
    published: TlFeatureRevision | None
    working: TlFeatureRevision | None
    extra: TlFeatureRevision | None  # revisione mergiata, se diversa dalla pubblicata


async def _fetch(
    client: TlGitClient,
    path: str,
    previous_path: str | None,
    commit_sha: str,
    allowed_extra: tuple[str, ...] | None = None,
) -> _Fetched | None:
    file = await client.get_file(path, commit_sha)
    if file is None:
        return None
    text = normalize_text(file.text)
    parsed = parse_feature_file(text, allowed_extra)
    return _Fetched(
        path,
        previous_path,
        commit_sha,
        text,
        file.blob_sha,
        parsed,
        tuple(allowed_extra) if allowed_extra is not None else None,
    )


async def _find_feature(db: AsyncSession, fetched: _Fetched) -> tuple[Any, ...] | None:
    """`(id, code, git_path)` per codice dell'header; altrimenti per percorso gestito."""
    cols = select(TlFeature.id, TlFeature.code, TlFeature.git_path)
    if fetched.parsed.code:
        row = (await db.execute(cols.where(TlFeature.code == fetched.parsed.code))).first()
        if row is not None:
            return tuple(row)
    paths = [p for p in (fetched.path, fetched.previous_path) if p]
    row = (await db.execute(cols.where(TlFeature.git_path.in_(paths)).limit(1))).first()
    return tuple(row) if row is not None else None


async def _is_copy(
    client: TlGitClient, code: str, git_path: str | None, fetched: _Fetched
) -> bool:
    """Stesso header mentre il file noto esiste ancora: è una copia, non uno spostamento."""
    if git_path is None or git_path in (fetched.path, fetched.previous_path):
        return False
    other = await client.get_file(git_path, fetched.commit_sha)
    return other is not None and parse_feature_file(other.text).code == code


async def _lock_all(
    db: AsyncSession, feature_id: uuid.UUID, extra_id: uuid.UUID | None = None
) -> _Locked:
    feature = await features_svc._lock_feature(db, feature_id)
    scope = await db.get(TlScope, feature.scope_id)
    assert scope is not None  # noqa: S101 - FK
    published = (
        await features_svc._lock_revision(db, feature.id, feature.published_revision_id)
        if feature.published_revision_id is not None
        else None
    )
    extra = None
    if extra_id is not None and extra_id != feature.published_revision_id:
        extra = await features_svc._lock_revision(db, feature.id, extra_id)
    working = None
    if feature.working_revision_id is not None:
        if extra is not None and feature.working_revision_id == extra.id:
            working = extra
        elif feature.working_revision_id != feature.published_revision_id:
            working = await features_svc._lock_revision(db, feature.id, feature.working_revision_id)
    return _Locked(feature, scope, published, working, extra)


def _change(kind: str, tag: str | None, message: str) -> dict[str, Any]:
    return {"kind": kind, "scenario_tag": tag, "message": message}


async def _open_structural(
    db: AsyncSession,
    feature: TlFeature,
    fetched: _Fetched,
    revision_id: uuid.UUID | None,
    changes: list[dict[str, Any]],
) -> None:
    """Una sola anomalia strutturale aperta per feature e percorso: le modifiche si accumulano.

    Le modifiche già presenti in un'anomalia riconosciuta non la riaprono a ogni reimport.
    """
    acknowledged = await issues_svc.find_open_issue(
        db,
        kind=TlIssueKind.STRUCTURAL_CHANGE,
        feature_id=feature.id,
        path=fetched.path,
        key=STRUCTURAL_KEY,
        statuses=(TlIssueStatus.ACKNOWLEDGED,),
    )
    if acknowledged is not None:
        known = (acknowledged.detail or {}).get("changes", [])
        changes = [c for c in changes if c not in known]
        if not changes:
            return
    existing = await issues_svc.find_open_issue(
        db,
        kind=TlIssueKind.STRUCTURAL_CHANGE,
        feature_id=feature.id,
        path=fetched.path,
        key=STRUCTURAL_KEY,
    )
    merged = list((existing.detail or {}).get("changes", [])) if existing else []
    for change in changes:
        if change not in merged:
            merged.append(change)
    await issues_svc.open_issue(
        db,
        kind=TlIssueKind.STRUCTURAL_CHANGE,
        feature_id=feature.id,
        revision_id=revision_id,
        path=fetched.path,
        commit_sha=fetched.commit_sha,
        detail={"key": STRUCTURAL_KEY, "changes": merged},
    )


async def _import_locked(
    db: AsyncSession,
    locked: _Locked,
    fetched: _Fetched,
    merged: TlFeatureRevision | None,
    copy: bool,
) -> None:
    """Passi 4-10 del brief, sotto lock. Nessun commit."""
    feature, parsed = locked.feature, fetched.parsed
    if parsed.errors:
        await issues_svc.open_issue(
            db,
            kind=TlIssueKind.PARSE_ERROR,
            feature_id=feature.id,
            path=fetched.path,
            commit_sha=fetched.commit_sha,
            detail={"key": "parse_error", "errors": parsed.errors},
        )
        return
    if copy:
        message = f"Il file ha lo stesso header di {feature.git_path}, che esiste ancora."
        await _open_structural(
            db, feature, fetched, None, [_change("duplicate_file", None, message)]
        )
        return

    moved_from = feature.git_path if feature.git_path != fetched.path else None
    feature.git_path = fetched.path
    feature.git_blob_sha = fetched.blob_sha
    if parsed.test_type and parsed.test_type != feature.test_type and locked.working is None:
        # the test type is declared in the file: Git wins, and the change is traced. Not while a
        # working revision exists: its test type is the draft's (or the reviewer's) to decide.
        feature.test_type = parsed.test_type
        history.record_event(
            db, feature.id, "tl.feature.updated", None, {"test_type": parsed.test_type}
        )
    for kind in (TlIssueKind.FILE_DELETED, TlIssueKind.PARSE_ERROR):
        await issues_svc.resolve_feature_issues(db, kind=kind, feature_id=feature.id)

    expected_rev = merged or locked.published
    if expected_rev is not None:
        render, _ = await history.build_render(db, feature, expected_rev)
    else:
        render = content.empty_render(feature, locked.scope.code)

    diff = DiffResult()
    valid: list[ParsedScenario] = []
    changed = False
    if expected_rev is None or normalize_text(render_feature_file(render)) != fetched.text:
        content.fill_defaults(parsed, render)
        diff = classify(
            render,
            parsed,
            scope_prefix=locked.scope.code,
            feature_number=feature.number,
            allowed_extra=fetched.allowed_extra,
        )
        valid = content.valid_scenarios(parsed, locked.scope.code, feature.number)
        changed = bool(diff.textual) or [s.identity_tag for s in valid] != [
            s.tag for s in render.scenarios
        ]

    # identità già note (anche se assenti dalla pubblicata): non sono "nuovi scenari"
    known_tags = set(
        (
            await db.execute(select(TlScenario.tag).where(TlScenario.feature_id == feature.id))
        ).scalars()
    )
    new_scenarios = [sc for sc in diff.new_scenarios if sc.identity_tag not in known_tags]
    target = expected_rev
    version_id = None
    if changed:
        if merged is None:
            target = await content.new_git_revision(
                db, feature, locked.published, fetched.commit_sha, parsed.title
            )
            if locked.published is not None:
                locked.published.status = TlRevisionStatus.SUPERSEDED
            feature.published_revision_id = target.id
        assert target is not None  # noqa: S101
        await content.apply_content(db, feature, target, parsed, valid)
        version = await history.snapshot(
            db, feature, target, None, origin="git", commit_sha=fetched.commit_sha
        )
        version_id = version.id

    target_id = target.id if target is not None else None
    for sc in new_scenarios:
        await issues_svc.open_issue(
            db,
            kind=TlIssueKind.NEW_SCENARIO_FROM_GIT,
            feature_id=feature.id,
            revision_id=target_id,
            path=fetched.path,
            commit_sha=fetched.commit_sha,
            detail={"key": sc.identity_tag, "tag": sc.identity_tag, "title": sc.title},
        )
    if diff.structural:
        await _open_structural(
            db,
            feature,
            fetched,
            target_id,
            [_change(c.kind, c.scenario_tag, c.message) for c in diff.structural],
        )
    if changed or moved_from:
        history.record_event(
            db,
            feature.id,
            "tl.git.imported",
            None,
            {
                "path": fetched.path,
                "previous_path": moved_from,
                "commit_sha": fetched.commit_sha,
                "revision_id": str(target_id) if changed else None,
                "version_id": str(version_id) if version_id else None,
                "textual": diff.textual,
                "structural": [c.kind for c in diff.structural],
                "new_scenarios": [sc.identity_tag for sc in new_scenarios],
            },
        )


async def _finalize(db: AsyncSession, locked: _Locked) -> None:
    """Passi 11-12: `implemented` se pubblicata e nessuna anomalia bloccante (regola unica in
    `issues.maybe_promote_feature`, rivalutata a ogni import), poi base superata."""
    feature = locked.feature
    await issues_svc.maybe_promote_feature(db, feature, None, reason="published_from_git")
    working = locked.working
    if (
        working is not None
        and feature.working_revision_id == working.id
        and working.base_revision_id != feature.published_revision_id
        and not working.base_stale
    ):
        working.base_stale = True
        previous = working.status
        if previous == TlRevisionStatus.APPROVED:
            working.status = TlRevisionStatus.DRAFT  # la PR aperta resta: aggiornata dopo il rebase
        history.record_event(
            db,
            feature.id,
            "tl.revision.stale",
            None,
            {
                "revision_id": str(working.id),
                "revision_number": working.number,
                "published_revision_id": str(feature.published_revision_id),
                "from": previous.value,
                "to": working.status.value,
            },
        )


async def import_changed_file(
    db: AsyncSession,
    path: str,
    previous_path: str | None,
    commit_sha: str,
    client: TlGitClient,
    merged_revision: TlFeatureRevision | None = None,
) -> None:
    """Importa il file `path` a `commit_sha` (passi 1-12 del brief). Un commit.

    La sessione viene annullata (rollback) prima delle chiamate di rete: il chiamante non deve
    avere modifiche pendenti. In caso di errore nulla è committato per questo file.

    Precondizione (Task 8): le PR mergiate si registrano prima con `mark_revision_merged`.
    Per difesa, se la revisione di lavoro ha una PR aperta che GitHub dà già mergiata, la si
    registra qui prima dell'import (nessuna revisione `git` spuria, nessuna base superata).

    `merged_revision` è interno: importa nel contenuto di quella revisione senza marcarla
    mergiata (usarlo solo tramite `mark_revision_merged`).
    """
    await db.rollback()  # nessuna transazione aperta durante la rete
    fetched = await _fetch(client, path, previous_path, commit_sha)
    if fetched is None:
        return  # non più presente a quel commit: ci pensa `handle_removed_file`
    found = await _find_feature(db, fetched)
    if found is None:
        if fetched.parsed.code:
            await issues_svc.open_issue(
                db,
                kind=TlIssueKind.UNKNOWN_FEATURE_CODE,
                path=path,
                commit_sha=commit_sha,
                detail={"key": fetched.parsed.code, "code": fetched.parsed.code},
            )
            await db.commit()
        return  # file non gestito (senza header): ignorato fino alla migrazione
    feature_id, code, git_path = found
    pending = (
        await db.execute(select(TlFeature.alignment_pending).where(TlFeature.id == feature_id))
    ).scalar_one()
    if pending and fetched.parsed.code is None:
        await db.rollback()
        return  # file migrato non ancora allineato (PR di allineamento aperta): nessuna anomalia
    merged_id = merged_revision.id if merged_revision is not None else None
    open_pr = None
    if merged_revision is None:
        open_pr = (
            await db.execute(
                select(TlFeatureRevision.id, TlFeatureRevision.pr_number)
                .join(TlFeature, TlFeature.working_revision_id == TlFeatureRevision.id)
                .where(
                    TlFeature.id == feature_id,
                    TlFeatureRevision.pr_state == "open",
                    TlFeatureRevision.pr_number.is_not(None),
                )
            )
        ).first()
    await db.rollback()  # nessuna transazione aperta durante la rete
    if open_pr is not None:
        pull = await client.get_pull(open_pr[1])
        if pull.state == "merged" and pull.merge_commit_sha:
            await _mark_merged(db, feature_id, open_pr[0], pull.merge_commit_sha, client)
    copy = await _is_copy(client, code, git_path, fetched)
    locked = await _lock_all(db, feature_id, merged_id)
    merged = locked.extra if merged_revision is not None else None
    if merged_revision is not None and merged is None:
        merged = locked.published  # la mergiata è già la pubblicata
    await _import_locked(db, locked, fetched, merged, copy)
    await _finalize(db, locked)
    await db.commit()


async def mark_revision_merged(
    db: AsyncSession, rev: TlFeatureRevision, merge_commit_sha: str, client: TlGitClient
) -> None:
    """La PR della revisione è mergiata: importa il file mergiato e la pubblica. Idempotente.

    Un commit; la sessione viene annullata prima della lettura del file da GitHub.
    """
    await _mark_merged(db, rev.feature_id, rev.id, merge_commit_sha, client)


async def _mark_merged(
    db: AsyncSession,
    feature_id: uuid.UUID,
    rev_id: uuid.UUID,
    merge_commit_sha: str,
    client: TlGitClient,
) -> None:
    git_path = (
        await db.execute(select(TlFeature.git_path).where(TlFeature.id == feature_id))
    ).scalar_one()
    await db.rollback()  # nessuna transazione aperta durante la rete
    fetched = await _fetch(client, git_path, None, merge_commit_sha) if git_path else None

    locked = await _lock_all(db, feature_id, rev_id)
    merged = locked.extra
    if merged is None or merged.pr_state == "merged":
        await db.rollback()  # già pubblicata
        return
    feature = locked.feature
    # stato di pubblicazione prima dell'import: lo snapshot `git` vede la revisione `merged`
    if locked.published is not None:
        locked.published.status = TlRevisionStatus.SUPERSEDED
    merged.status = TlRevisionStatus.MERGED
    merged.pr_state = "merged"
    merged.merged_commit_sha = merge_commit_sha
    merged.base_stale = False
    feature.published_revision_id = merged.id
    if feature.working_revision_id == merged.id:
        feature.working_revision_id = None
    if fetched is not None:
        await _import_locked(db, locked, fetched, merged, copy=False)
    elif git_path:
        # il file non c'è al commit di merge: incoerente finché un import non lo ritrova
        await issues_svc.open_issue(
            db,
            kind=TlIssueKind.FILE_DELETED,
            feature_id=feature.id,
            revision_id=merged.id,
            path=git_path,
            commit_sha=merge_commit_sha,
            detail={"key": "file_deleted", "path": git_path},
        )
    history.record_event(
        db,
        feature.id,
        "tl.pr.merged",
        None,
        {
            "revision_id": str(merged.id),
            "revision_number": merged.number,
            "pr_number": merged.pr_number,
            "merged_commit_sha": merge_commit_sha,
        },
    )
    await _finalize(db, locked)
    await db.commit()


async def handle_removed_file(
    db: AsyncSession,
    path: str,
    commit_sha: str,
    client: TlGitClient | None = None,
    present_paths: Iterable[str] = (),
) -> None:
    """Un file gestito è stato cancellato: anomalia `file_deleted`. Un commit.

    Spostamento: `present_paths` sono i file aggiunti/modificati/rinominati dello stesso
    compare. Se uno di questi a `commit_sha` ha lo stesso header della feature non si apre
    nulla (l'import di quel file aggiorna `git_path`), in qualunque ordine li tratti il sync.
    Se il file nuovo è già stato importato, `git_path` punta già altrove e qui non succede nulla.
    """
    row = (
        await db.execute(
            select(TlFeature.id, TlFeature.code).where(TlFeature.git_path == path).limit(1)
        )
    ).first()
    await db.rollback()  # nessuna transazione aperta durante la rete
    if row is None:
        return
    feature_id, code = row
    if client is not None:
        for other in present_paths:
            if other == path or not other.endswith(".feature"):
                continue
            file = await client.get_file(other, commit_sha)
            if file is not None and parse_feature_file(file.text).code == code:
                return  # spostato nello stesso compare
    feature = await features_svc._lock_feature(db, feature_id)
    if feature.git_path != path:
        await db.rollback()
        return
    await issues_svc.open_issue(
        db,
        kind=TlIssueKind.FILE_DELETED,
        feature_id=feature.id,
        revision_id=feature.published_revision_id,
        path=path,
        commit_sha=commit_sha,
        detail={"key": "file_deleted", "path": path},
    )
    await db.commit()

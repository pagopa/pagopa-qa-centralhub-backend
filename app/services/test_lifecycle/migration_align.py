"""PR di allineamento della migrazione (fase 5): un branch e una PR per ambito.

Riscrive i feature file migrati nel formato dell'hub (header `# qa-hub:`, tag identità nuovi,
tag di categoria mantenuti) senza spostarli. Il merge lo fa il team su GitHub; il sync chiude il
giro (Task 7).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlMigrationItem,
    TlMigrationScope,
)
from app.schemas.test_lifecycle import TlMigrationScopeOut
from app.services.test_lifecycle import history
from app.services.test_lifecycle.feature_parser import parse_feature_file
from app.services.test_lifecycle.gherkin import render_feature_file
from app.services.test_lifecycle.git_client import TlGitError

logger = logging.getLogger(__name__)


def branch_for(scope_code: str) -> str:
    return f"qa-hub/migration-{scope_code.lower()}"


def lost_comments(original: str, rendered: str) -> list[str]:
    """Commenti del file originale che il file rigenerato non contiene più."""
    kept = {line.strip() for line in rendered.splitlines()}
    out = []
    for line in original.splitlines():
        s = line.strip()
        if not s.startswith("#") or s.startswith(("# language:", "# qa-hub:")):
            continue
        if s not in kept and s not in out:
            out.append(s)
    return out


def _body(scope_code: str, files: list[dict[str, Any]]) -> str:
    lines = [
        f"Allineamento dei feature file dell'ambito **{scope_code}** allo standard di QA Hub",
        "(migrazione da Confluence). I file restano dove sono; cambiano header e tag identità.",
        "I tag di categoria restano; i vecchi tag identità sono salvati come alias nell'hub,",
        "così lo storico NRT resta collegato.",
        "",
    ]
    for f in files:
        lines.append(f"### `{f['path']}` — {f['code']}")
        if f["renames"]:
            lines += ["", "| Tag precedente | Tag nuovo |", "| --- | --- |"]
            lines += [f"| `{old}` | `{new}` |" for old, new in f["renames"]]
        if f["lost"]:
            lines += ["", "Commenti non riportati nel file rigenerato:", ""]
            lines += [f"- `{c}`" for c in f["lost"]]
        lines.append("")
    if settings.tl_hub_base_url:
        lines.append(f"Migrazione su QA Hub: {settings.tl_hub_base_url}/test-lifecycle/migrazione")
    return "\n".join(lines)


async def scope_out(db: AsyncSession, scope_code: str) -> TlMigrationScopeOut:
    from app.services.test_lifecycle.migration import summary

    for s in (await summary(db)).scopes:
        if s.scope_code.lower() == scope_code.lower():
            return s
    return TlMigrationScopeOut(scope_code=scope_code)


async def _get_scope_row(db: AsyncSession, scope_code: str) -> TlMigrationScope:
    row = (
        await db.execute(
            select(TlMigrationScope).where(
                func.lower(TlMigrationScope.scope_code) == scope_code.lower()
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = TlMigrationScope(scope_code=scope_code, confluence_errors=[])
        db.add(row)
        await db.flush()
    return row


async def _targets(db: AsyncSession, scope_code: str) -> list[dict[str, Any]]:
    """Files of the scope's migrated features still waiting for their alignment pull request."""
    rows = (
        await db.execute(
            select(TlFeature, TlFeatureRevision, TlMigrationItem)
            .join(TlMigrationItem, TlMigrationItem.feature_id == TlFeature.id)
            .join(TlFeatureRevision, TlFeatureRevision.id == TlFeature.published_revision_id)
            .where(
                TlMigrationItem.executed_at.is_not(None),
                TlFeature.alignment_pending.is_(True),
                TlFeature.git_path.is_not(None),
            )
            .order_by(TlFeature.number)
        )
    ).all()
    targets: list[dict[str, Any]] = []
    for feature, rev, item in rows:
        if str(item.overrides.get("scope_code") or item.scope_code).lower() != scope_code.lower():
            continue
        render, contents = await history.build_render(db, feature, rev)
        old = item.proposal.get("old_scenario_tags") or []
        renames = [(o, s.tag) for o, (s, _c) in zip(old, contents, strict=False) if o]
        targets.append(
            {
                "path": feature.git_path,
                "code": feature.code,
                "text": render_feature_file(render),
                "renames": renames,
            }
        )
    return targets


async def open_alignment_pr(db: AsyncSession, scope_code: str, git: Any) -> TlMigrationScopeOut:
    """Crea o aggiorna branch e PR di allineamento dell'ambito. Errori salvati in `pr_error`."""
    targets = await _targets(db, scope_code)
    scope_row = await _get_scope_row(db, scope_code)
    await db.commit()
    if not targets:
        return await scope_out(db, scope_code)

    branch = branch_for(scope_code)
    try:
        base = settings.tl_base_branch
        head = await git.head_sha(base)
        if not await git.branch_exists(branch):
            await git.create_branch(branch, head)
        for t in targets:
            original = await git.get_file(t["path"], head)
            t["lost"] = lost_comments(original.text, t["text"]) if original else []
            current = await git.get_file(t["path"], branch)
            if current is not None and current.text == t["text"]:
                continue
            await git.put_file(
                t["path"],
                t["text"],
                branch,
                f"chore(qa-hub): allinea {t['code']} allo standard QA Hub",
                current.blob_sha if current else None,
            )
        pull = await git.find_open_pull(branch)
        if pull is None:
            pull = await git.create_pull(
                branch, base, f"Allineamento QA Hub — {scope_code}", _body(scope_code, targets)
            )
    except TlGitError as exc:
        scope_row = await _get_scope_row(db, scope_code)
        scope_row.pr_error = exc.message[:2000]
        await db.commit()
        return await scope_out(db, scope_code)
    scope_row = await _get_scope_row(db, scope_code)
    scope_row.pr_number, scope_row.pr_url, scope_row.pr_state = pull.number, pull.url, "open"
    scope_row.pr_error = None
    scope_row.updated_at = datetime.now(UTC)
    await db.commit()
    return await scope_out(db, scope_code)


async def _pending_of_scope(db: AsyncSession, scope_code: str) -> list[tuple[Any, str, str | None]]:
    """`(feature id, code, git path)` of the pending features whose migration scope is this one."""
    rows = (
        await db.execute(
            select(TlFeature.id, TlFeature.code, TlFeature.git_path, TlMigrationItem)
            .join(TlMigrationItem, TlMigrationItem.feature_id == TlFeature.id)
            .where(TlFeature.alignment_pending.is_(True))
        )
    ).all()
    return [
        (fid, code, path)
        for fid, code, path, item in rows
        if str(item.overrides.get("scope_code") or item.scope_code).lower() == scope_code.lower()
    ]


async def _aligned_on_main(
    git: Any, candidates: list[tuple[Any, str, str | None]], ref: str
) -> tuple[list[Any], list[str]]:
    """Which candidates really carry their `# qa-hub:` header at `ref` (the merge commit).

    A merged pull request does not prove that a feature's file was aligned: it may not have
    carried it (the feature joined the scope later, the refresh failed). Only the files that show
    the feature's code in their header are aligned; the others stay pending.
    """
    aligned: list[Any] = []
    missed: list[str] = []
    for fid, code, path in candidates:
        try:
            file = await git.get_file(path, ref) if path else None
        except TlGitError as exc:
            logger.warning("Verifica allineamento di %s non riuscita: %s", path, exc.message)
            file = None
        if file is not None and parse_feature_file(file.text).code == code:
            aligned.append(fid)
        else:
            missed.append(path or code)
    return aligned, missed


async def sync_alignment_prs(db: AsyncSession, git: Any) -> None:
    """Dal sync: PR di allineamento mergiate (feature non più in attesa) o chiuse (errore)."""
    rows = list(
        (
            await db.execute(
                select(TlMigrationScope.scope_code, TlMigrationScope.pr_number).where(
                    TlMigrationScope.pr_state == "open", TlMigrationScope.pr_number.is_not(None)
                )
            )
        ).all()
    )
    await db.rollback()
    for scope_code, number in rows:
        try:
            pull = await git.get_pull(number)
        except TlGitError as exc:
            logger.warning("PR di allineamento %s non leggibile: %s", number, exc.message)
            continue
        if pull.state == "open":
            continue
        row = await _get_scope_row(db, scope_code)
        if pull.state == "merged":
            row.pr_state, row.pr_error = "merged", None
            candidates = await _pending_of_scope(db, scope_code)
            await db.commit()
            aligned, missed = await _aligned_on_main(
                git, candidates, pull.merge_commit_sha or settings.tl_base_branch
            )
            if missed:
                row = await _get_scope_row(db, scope_code)
                row.pr_error = (
                    "PR mergiata ma questi file non sono allineati su main: " + ", ".join(missed)
                )[:2000]
            features = (
                await db.execute(
                    select(TlFeature)
                    .where(TlFeature.id.in_(aligned), TlFeature.alignment_pending.is_(True))
                    .with_for_update(of=TlFeature)
                )
            ).scalars()
            for feature in features:
                feature.alignment_pending = False
                history.record_event(
                    db,
                    feature.id,
                    "tl.feature.aligned",
                    None,
                    {"pr_number": number, "merge_commit_sha": pull.merge_commit_sha},
                )
        else:
            row.pr_state, row.pr_error = "closed", "PR chiusa senza merge"
        await db.commit()


async def retire_alignment_pr(db: AsyncSession, scope_code: str, git: Any) -> str | None:
    """After features left `scope_code`: its open alignment pull request stops carrying them.

    The files that are no longer the scope's get their `main` content back on the branch; when
    nothing is left to carry the pull request is closed, otherwise it is refreshed with the
    remaining features. Returns an error message, or `None`.
    """
    row = (
        await db.execute(
            select(TlMigrationScope).where(
                func.lower(TlMigrationScope.scope_code) == scope_code.lower()
            )
        )
    ).scalar_one_or_none()
    if row is None or row.pr_state != "open" or row.pr_number is None:
        await db.rollback()
        return None
    number = row.pr_number
    targets = await _targets(db, scope_code)
    await db.commit()
    branch = branch_for(row.scope_code)
    try:
        base = settings.tl_base_branch
        head = await git.head_sha(base)
        if await git.branch_exists(branch):
            keep = {t["path"] for t in targets}
            for changed in await git.compare(head, await git.head_sha(branch)):
                if changed.path in keep:
                    continue
                original = await git.get_file(changed.path, head)
                current = await git.get_file(changed.path, branch)
                if current is None:
                    continue
                if original is None:
                    await git.delete_file(
                        changed.path,
                        branch,
                        "chore(qa-hub): togli il file spostato",
                        current.blob_sha,
                    )
                elif current.text != original.text:
                    await git.put_file(
                        changed.path,
                        original.text,
                        branch,
                        "chore(qa-hub): ripristina il file spostato in un altro ambito",
                        current.blob_sha,
                    )
        if not targets:
            await git.close_pull(number)
    except TlGitError as exc:
        row = await _get_scope_row(db, scope_code)
        row.pr_error = exc.message[:2000]
        await db.commit()
        return f"PR di allineamento {scope_code}: {exc.message}"
    row = await _get_scope_row(db, scope_code)
    if not targets:
        row.pr_state, row.pr_error = "closed", "PR chiusa: nessuna feature da allineare"
        row.updated_at = datetime.now(UTC)
        await db.commit()
        return None
    await db.commit()
    out = await open_alignment_pr(db, scope_code, git)
    return out.pr_error


async def refresh_after_change(
    db: AsyncSession, git: Any, moves: list[tuple[str, str]]
) -> str | None:
    """After a move or a scope rename: update the pull requests of the migration alignment.

    `moves`: `(old scope code, new scope code)`. The destination's pull request is created or
    updated from the hub content, the source's is retired. A Git problem never undoes the change
    already made: it is returned as a message (and kept in the scope's `pr_error`).
    """
    if git is None:
        # only worth a message when there is something to refresh
        for old_code, new_code in moves:
            row = (
                await db.execute(
                    select(TlMigrationScope.pr_state).where(
                        func.lower(TlMigrationScope.scope_code) == old_code.lower()
                    )
                )
            ).scalar_one_or_none()
            if row == "open" or await _targets(db, new_code):
                await db.rollback()
                return (
                    "Git non configurato: le PR di allineamento della migrazione "
                    "non sono state aggiornate"
                )
        await db.rollback()
        return None
    errors: list[str] = []
    for old_code, new_code in dict.fromkeys(moves):
        out = await open_alignment_pr(db, new_code, git)
        if out.pr_error:
            errors.append(f"PR di allineamento {new_code}: {out.pr_error}")
        if old_code.lower() != new_code.lower():
            error = await retire_alignment_pr(db, old_code, git)
            if error:
                errors.append(error)
    return "; ".join(errors) or None

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


async def open_alignment_pr(db: AsyncSession, scope_code: str, git: Any) -> TlMigrationScopeOut:
    """Crea o aggiorna branch e PR di allineamento dell'ambito. Errori salvati in `pr_error`."""
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

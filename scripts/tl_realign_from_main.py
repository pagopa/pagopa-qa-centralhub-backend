"""Rebuild the Test Lifecycle features whose file on main lost the hub header (main wins).

Usage:
    uv run python -m scripts.tl_realign_from_main                 # dry run: report only
    uv run python -m scripts.tl_realign_from_main --apply         # rebuild and reopen the PRs
    uv run python -m scripts.tl_realign_from_main --code FEAT_001_WISP [--apply]

A feature is a candidate when it is not waiting for alignment and its published revision has no
scenario, or it has an open structural anomaly. Cases that are not clear-cut (a different number
of scenarios in the file and in the hub, a file that already has the header, a draft with work in
it) are reported as "skipped" and left untouched. With
--apply, the alignment pull request of every touched scope is created or updated afterwards.
Needs the database and GITHUB_TOKEN.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import func, or_, select

from app.core.db import async_session
from app.models.test_lifecycle import (
    TlFeature,
    TlIssue,
    TlIssueKind,
    TlIssueStatus,
    TlScenarioContent,
    TlScope,
)
from app.services.test_lifecycle import migration_align, realign
from app.services.test_lifecycle.git_client import get_git_client


async def _candidates(code: str | None) -> list[tuple]:
    """Features not waiting for alignment whose published revision is empty or that have an
    open structural anomaly; `realign_from_main` decides which of them can be rebuilt."""
    async with async_session() as db:
        scenarios = (
            select(func.count())
            .select_from(TlScenarioContent)
            .where(TlScenarioContent.revision_id == TlFeature.published_revision_id)
            .scalar_subquery()
        )
        open_structural = (
            select(TlIssue.id)
            .where(
                TlIssue.feature_id == TlFeature.id,
                TlIssue.kind == TlIssueKind.STRUCTURAL_CHANGE,
                TlIssue.status == TlIssueStatus.OPEN,
            )
            .exists()
        )
        stmt = (
            select(TlFeature.id, TlFeature.code, TlScope.code)
            .join(TlScope, TlScope.id == TlFeature.scope_id)
            .where(
                TlFeature.published_revision_id.is_not(None),
                TlFeature.git_path.is_not(None),
                TlFeature.alignment_pending.is_(False),
                or_(scenarios == 0, open_structural),
            )
            .order_by(TlScope.code, TlFeature.number)
        )
        if code:
            stmt = stmt.where(TlFeature.code == code)
        return [tuple(r) for r in (await db.execute(stmt)).all()]


async def _main(apply: bool, code: str | None) -> int:
    git = get_git_client()
    candidates = await _candidates(code)
    if not candidates:
        print("Nessuna feature da recuperare.")
        return 0
    touched: set[str] = set()
    for fid, fcode, scope_code in candidates:
        async with async_session() as db:
            report = await realign.realign_from_main(db, git, fid, apply=apply)
        detail = report.reason or (
            f"{report.scenarios} scenari, {report.changed_scenarios} con testo diverso, "
            f"revisione base {report.baseline_revision}"
        )
        print(f"[{report.status}] {fcode}: {detail}")
        if report.status == "applied":
            touched.add(scope_code)
    for scope_code in sorted(touched):
        async with async_session() as db:
            out = await migration_align.open_alignment_pr(db, scope_code, git)
        print(f"PR di allineamento {scope_code}: {out.pr_url or out.pr_error}")
    if not apply:
        print("\nDry run: nessuna modifica. Rilancia con --apply per applicare.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--code", help="only this feature code, e.g. FEAT_001_WISP")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.apply, args.code)))


if __name__ == "__main__":
    main()

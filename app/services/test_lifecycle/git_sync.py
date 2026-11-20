"""Sync periodico con Git: stato delle PR, import dei file cambiati su main, indice degli step.

Un solo sync alla volta (lock advisory globale `tl_git_sync`, `SyncInProgress` se occupato).
Ordine: (1) stato delle PR aperte, (2) file presenti (aggiunti/modificati/rinominati), (3) file
rimossi. Ogni funzione di `git_import` gestisce le proprie transazioni: qui non si apre nulla.
Se il giro fallisce `last_main_sha` resta invariato, così il prossimo riprova lo stesso range.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.test_lifecycle import TlFeatureRevision, TlGitSyncState, TlIssue, TlIssueKind
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import git_import, history
from app.services.test_lifecycle import issues as issues_svc
from app.services.test_lifecycle.advisory_lock import try_advisory_lock
from app.services.test_lifecycle.git_client import TlGitClient, TlGitError
from app.services.test_lifecycle.step_index import rebuild_step_index

logger = logging.getLogger(__name__)

LOCK_KEY = "tl_git_sync"
UNEXPECTED_ERROR = "Errore imprevisto durante la sincronizzazione con Git"
_STEPS_PY = re.compile(r"(^|/)steps/(.+/)?[^/]+\.py$")


class SyncInProgress(Exception):  # noqa: N818 - name fixed by the interface
    """Un altro sync è già in corso."""


@dataclass
class SyncSummary:
    prs_merged: int
    prs_closed: int
    files_imported: int
    issues_opened: int
    steps_indexed: bool
    head_sha: str


async def _record_error(db: AsyncSession, message: str) -> None:
    await db.rollback()
    values = {"id": 1, "last_run_at": datetime.now(UTC), "last_error": message}
    stmt = pg_insert(TlGitSyncState).values(**values)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[TlGitSyncState.id],
            set_={"last_run_at": values["last_run_at"], "last_error": message},
        )
    )
    await db.commit()


async def _save_state(db: AsyncSession, summary: SyncSummary) -> None:
    values = {
        "id": 1,
        "last_main_sha": summary.head_sha,
        "last_run_at": datetime.now(UTC),
        "last_error": None,
        "last_summary": asdict(summary),
    }
    stmt = pg_insert(TlGitSyncState).values(**values)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[TlGitSyncState.id],
            set_={k: v for k, v in values.items() if k != "id"},
        )
    )
    await db.commit()


async def _count_issues(db: AsyncSession) -> int:
    n = (await db.execute(select(func.count()).select_from(TlIssue))).scalar_one()
    await db.rollback()
    return n


async def _close_unmerged(db: AsyncSession, feature_id: uuid.UUID, rev_id: uuid.UUID) -> bool:
    """PR chiusa senza merge: stato, evento e anomalia (ordine lock feature -> revisione)."""
    feature = await features_svc._lock_feature(db, feature_id)
    rev = await features_svc._lock_revision(db, feature_id, rev_id)
    if rev.pr_state != "open":
        await db.rollback()
        return False
    rev.pr_state = "closed"
    history.record_event(
        db,
        feature.id,
        "tl.pr.closed",
        None,
        {
            "revision_id": str(rev.id),
            "revision_number": rev.number,
            "pr_number": rev.pr_number,
        },
    )
    await issues_svc.open_issue(
        db,
        kind=TlIssueKind.PR_CLOSED_UNMERGED,
        feature_id=feature.id,
        revision_id=rev.id,
        detail={"key": str(rev.id), "pr_number": rev.pr_number, "revision_number": rev.number},
    )
    await db.commit()
    return True


async def _sync_pull_requests(db: AsyncSession, client: TlGitClient) -> tuple[int, int]:
    rows = (
        await db.execute(
            select(TlFeatureRevision.id, TlFeatureRevision.feature_id, TlFeatureRevision.pr_number)
            .where(TlFeatureRevision.pr_state == "open", TlFeatureRevision.pr_number.is_not(None))
            .order_by(TlFeatureRevision.created_at)
        )
    ).all()
    await db.rollback()
    merged = closed = 0
    for rev_id, feature_id, number in rows:
        pull = await client.get_pull(number)
        if pull.state == "merged" and pull.merge_commit_sha:
            rev = await db.get(TlFeatureRevision, rev_id)
            if rev is None or rev.pr_state != "open":
                await db.rollback()
                continue
            await git_import.mark_revision_merged(db, rev, pull.merge_commit_sha, client)
            merged += 1
        elif pull.state == "closed":
            if await _close_unmerged(db, feature_id, rev_id):
                closed += 1
    return merged, closed


def _touches_steps(paths: list[str]) -> bool:
    return any(p.endswith(".feature") or _STEPS_PY.search(p) for p in paths)


async def _run_locked(db: AsyncSession, client: TlGitClient) -> SyncSummary:
    last_sha = (
        await db.execute(select(TlGitSyncState.last_main_sha).where(TlGitSyncState.id == 1))
    ).scalar_one_or_none()
    issues_before = await _count_issues(db)  # chiude anche la transazione prima della rete
    head = await client.head_sha(settings.tl_base_branch)
    prs_merged, prs_closed = await _sync_pull_requests(db, client)

    files_imported = 0
    steps_indexed = False
    if last_sha is None:
        steps_indexed = True  # primo giro: nessun import, ma l'indice degli step parte
    elif last_sha != head:
        changes = await client.compare(last_sha, head)
        present = [c for c in changes if c.status != "removed"]
        present_features = [c.path for c in present if c.path.endswith(".feature")]
        for change in present:
            if change.path.endswith(".feature"):
                await git_import.import_changed_file(
                    db, change.path, change.previous_path, head, client
                )
                files_imported += 1
        for change in changes:
            if change.status == "removed" and change.path.endswith(".feature"):
                await git_import.handle_removed_file(
                    db, change.path, head, client, present_features
                )
        steps_indexed = _touches_steps(
            [c.path for c in changes]
            + [c.previous_path for c in changes if c.previous_path]
        )
    if steps_indexed:
        await rebuild_step_index(db, client, head)

    summary = SyncSummary(
        prs_merged=prs_merged,
        prs_closed=prs_closed,
        files_imported=files_imported,
        issues_opened=max(await _count_issues(db) - issues_before, 0),
        steps_indexed=steps_indexed,
        head_sha=head,
    )
    await _save_state(db, summary)
    return summary


async def run_sync(db: AsyncSession, client: TlGitClient) -> SyncSummary:
    """Un giro di sync. `SyncInProgress` se un altro è in corso; su errore salva `last_error`."""
    async with try_advisory_lock(db, LOCK_KEY) as got:
        if not got:
            raise SyncInProgress()
        try:
            return await _run_locked(db, client)
        except TlGitError as exc:
            await _record_error(db, exc.message)
            raise
        except Exception:
            logger.exception("sync git del Test Lifecycle fallita")
            await _record_error(db, UNEXPECTED_ERROR)
            raise

"""Glue between the sync engine, the real sources and a database session."""

from __future__ import annotations

import uuid
from typing import Any

from app.services.ea.sync.engine import execute_run
from app.services.ea.sync.sources import (
    ConfluenceClient,
    GitHubClient,
    SourceError,
    default_confluence,
    default_github,
)


async def run_with_default_sources(run_id: uuid.UUID, *, dry_run: bool = False) -> dict[str, Any]:
    """Execute a registered run with GitHub and Confluence clients built from settings.

    Confluence is optional: without Atlassian credentials the run still updates GitHub
    data and ends as ``partial``.
    """
    from app.core.db import async_session

    github: GitHubClient | None = None
    confluence: ConfluenceClient | None = None
    async with async_session() as db:
        try:
            try:
                github = default_github()
            except SourceError as exc:
                return await _fail(db, run_id, str(exc))
            try:
                confluence = default_confluence()
            except SourceError:
                confluence = None
            return await execute_run(db, run_id, github, confluence, dry_run=dry_run)
        finally:
            if github is not None:
                await github.aclose()
            if confluence is not None:
                await confluence.aclose()


async def _fail(db: Any, run_id: uuid.UUID, error: str) -> dict[str, Any]:
    from datetime import UTC, datetime

    from app.models.ea import EaSyncRun
    from app.services.ea.sync.engine import _record_status

    run = await db.get(EaSyncRun, run_id)
    if run is None:
        return {"run_id": str(run_id), "status": "failed", "error": error, "stats": {}}
    run.status, run.error, run.finished_at = "failed", error, datetime.now(UTC)
    await _record_status(db, run)
    await db.commit()
    return {"run_id": str(run_id), "status": "failed", "error": error, "stats": {}}

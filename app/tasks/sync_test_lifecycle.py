from __future__ import annotations

from dataclasses import asdict
from typing import Any

import structlog

from app.tasks import celery_app
from app.tasks.async_runner import run_async_task

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.sync_test_lifecycle.sync_test_lifecycle_git")  # type: ignore[untyped-decorator]
def sync_test_lifecycle_git() -> dict[str, Any]:
    return run_async_task(_async_sync())


async def _async_sync() -> dict[str, Any]:
    from app.core.db import async_session
    from app.services.test_lifecycle import git_client, git_sync

    try:
        client = git_client.get_git_client()
    except git_client.TlGitNotConfigured:
        logger.info("tl_git_sync_skipped", reason="git_not_configured")
        return {"status": "not_configured"}
    try:
        async with async_session() as db:
            summary = await git_sync.run_sync(db, client)
    except git_sync.SyncInProgress:
        logger.info("tl_git_sync_skipped", reason="sync_in_progress")
        return {"status": "busy"}
    except git_client.TlGitError as exc:
        logger.warning("tl_git_sync_failed", error=exc.message)  # last_error già salvato
        return {"status": "error", "error": exc.message}
    except Exception:
        logger.exception("tl_git_sync_failed")
        return {"status": "error", "error": "unexpected"}
    logger.info("tl_git_sync_finished", **asdict(summary))
    return {"status": "ok", **asdict(summary)}

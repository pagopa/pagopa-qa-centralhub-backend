"""Run the EA Map sync from the command line (same code as the weekly Celery task).

Usage:
    uv run python -m scripts.ea_sync              # real run: applies and queues changes
    uv run python -m scripts.ea_sync --dry-run    # computes everything, writes nothing

Needs GITHUB_TOKEN; JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN for Confluence (optional).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter

from app.core.db import async_session
from app.services.ea.sync.engine import EaSyncBusyError, create_run
from app.services.ea.sync.runner import run_with_default_sources


async def _main(dry_run: bool) -> int:
    async with async_session() as db:
        try:
            run = await create_run(db, trigger="manual", actor="cli")
        except EaSyncBusyError as exc:
            print(exc)
            return 1
    summary = await run_with_default_sources(run.id, dry_run=dry_run)
    proposals = summary.pop("proposals", [])
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    if proposals:
        print("\nProposte:", dict(Counter(f"{p['entity']}.{p['op']}" for p in proposals)))
        for p in proposals[:80]:
            print(f"  [{p['status']}] {p['entity']}.{p['op']} {p['target']}")
    return 0 if summary["status"] in ("success", "partial") else 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="compute without writing")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.dry_run)))


if __name__ == "__main__":
    main()

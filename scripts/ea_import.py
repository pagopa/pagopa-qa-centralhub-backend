"""Import the EA Map knowledge base into PostgreSQL.

Usage:
    uv run python -m scripts.ea_import                      # default seed
    uv run python -m scripts.ea_import path/to/kb.json --replace
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.core.db import async_session
from app.services.ea.importer import import_kb

DEFAULT_SEED = Path(__file__).parent / "data" / "ea_kb_seed.json"


async def _main(path: Path, replace: bool) -> None:
    kb = json.loads(path.read_text(encoding="utf-8"))
    async with async_session() as db:
        stats = await import_kb(db, kb, replace=replace)
    print(f"EA Map imported from {path}: {stats}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--replace", action="store_true", help="wipe EA tables before importing")
    args = parser.parse_args()
    asyncio.run(_main(args.path, args.replace))


if __name__ == "__main__":
    main()

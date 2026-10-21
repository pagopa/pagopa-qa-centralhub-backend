from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


def run_async_task[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(_run_and_dispose(coroutine))


async def _run_and_dispose[T](coroutine: Coroutine[Any, Any, T]) -> T:
    from app.core.db import engine

    try:
        return await coroutine
    finally:
        # Pooled asyncpg connections must close on their owning loop, before asyncio.run exits.
        await engine.dispose()

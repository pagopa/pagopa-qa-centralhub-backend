from __future__ import annotations

# ruff: noqa: S101
import asyncio
import importlib

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlalchemy.util.concurrency import greenlet_spawn

TASKS = [
    ("sync_ea", "sync_ea"),
    ("sync_sanp_health", "sync_sanp_health"),
    ("sync_e2e", "sync_e2e_runs"),
    ("sync_gpd_position", "sync_gpd_position_snapshots"),
    ("sync_psp_fee", "sync_psp_fee_services"),
]


class LoopBoundConnection:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()

    async def query(self) -> None:
        future = self.loop.create_future()
        if not self.loop.is_closed():
            self.loop.call_soon(future.set_result, None)
        await future

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        assert asyncio.get_running_loop() is self.loop


@pytest.mark.parametrize(("module_name", "task_name"), TASKS)
@pytest.mark.parametrize("first_fails", [False, True])
def test_consecutive_worker_tasks_do_not_reuse_connections_across_loops(
    monkeypatch: pytest.MonkeyPatch, module_name: str, task_name: str, first_fails: bool,
) -> None:
    module = importlib.import_module(f"app.tasks.{module_name}")
    pool = AsyncAdaptedQueuePool(LoopBoundConnection)
    calls = 0

    async def sync(*args: object, **kwargs: object) -> dict[str, str]:
        nonlocal calls
        calls += 1
        connection = await greenlet_spawn(pool.connect)
        try:
            await connection.dbapi_connection.query()
        finally:
            await greenlet_spawn(connection.close)
        if first_fails and calls == 1:
            raise RuntimeError("source failed")
        return {"status": "success"}

    async def dispose(self: AsyncEngine, close: bool = True) -> None:
        await greenlet_spawn(pool.dispose)

    monkeypatch.setattr(module, "_async_sync", sync)
    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    task = getattr(module, task_name)
    if first_fails:
        with pytest.raises(RuntimeError, match="source failed"):
            task.run()
    else:
        assert task.run() == {"status": "success"}
    assert task.run() == {"status": "success"}

    next_module_name = "sync_sanp_health" if module_name == "sync_ea" else "sync_ea"
    next_module = importlib.import_module(f"app.tasks.{next_module_name}")
    monkeypatch.setattr(next_module, "_async_sync", sync)
    assert getattr(next_module, next_module_name).run() == {"status": "success"}

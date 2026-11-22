from __future__ import annotations

# ruff: noqa: S101
from app.tasks import celery_app, schedule  # noqa: F401  (schedule populates beat_schedule)


def test_messaging_tasks_are_registered_with_the_worker() -> None:
    assert "app.tasks.messaging.dispatch_messages" in celery_app.tasks
    assert "app.tasks.messaging.purge_messages" in celery_app.tasks
    assert "app.tasks.messaging.scan_conditions" in celery_app.tasks


def test_messaging_tasks_are_scheduled() -> None:
    tasks = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert "app.tasks.messaging.dispatch_messages" in tasks
    assert "app.tasks.messaging.purge_messages" in tasks
    assert "app.tasks.messaging.scan_conditions" in tasks

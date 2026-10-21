from __future__ import annotations

# ruff: noqa: S101
from pathlib import Path
from queue import Empty
from uuid import uuid4

import pytest
import yaml
from celery.beat import ScheduleEntry, Scheduler
from kombu import Connection, Producer

from app.config import Settings, settings
from app.tasks import celery_app, schedule
from app.tasks.sync_ea import sync_ea


def test_worker_consumes_only_the_configured_queue() -> None:
    queues = celery_app.amqp.queues.consume_from
    assert set(queues) == {settings.celery_queue}
    assert settings.celery_queue != "celery"
    queue = queues[settings.celery_queue]
    assert queue.exchange.name == settings.celery_queue
    assert queue.routing_key == settings.celery_queue


def test_queue_can_be_overridden_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELERY_QUEUE", "qa-centralhub-custom")
    assert Settings().celery_queue == "qa-centralhub-custom"


@pytest.mark.parametrize("entry_name", [None, *schedule.celery_app.conf.beat_schedule])
def test_manual_and_beat_tasks_reach_the_worker_queue(entry_name: str | None) -> None:
    run_id = str(uuid4())
    with Connection("memory://") as connection:
        producer = Producer(connection)
        if entry_name is None:
            task_name = sync_ea.name
            sync_ea.apply_async(args=[run_id], producer=producer, ignore_result=True)
        else:
            config = schedule.celery_app.conf.beat_schedule[entry_name]
            entry = ScheduleEntry(
                name=entry_name,
                app=celery_app,
                **{
                    **config,
                    "options": {**config.get("options", {}), "ignore_result": True},
                },
            )
            task_name = entry.task
            Scheduler(app=celery_app, lazy=True).apply_async(
                entry, producer=producer, advance=False,
            )

        with connection.SimpleQueue(settings.celery_queue) as queue:
            message = queue.get(block=False)
            assert message.headers["task"] == task_name
            assert message.delivery_info["routing_key"] == settings.celery_queue
            if entry_name is None:
                assert message.payload[0] == [run_id]
            message.ack()

        with connection.SimpleQueue("celery") as shared_queue:
            with pytest.raises(Empty):
                shared_queue.get(block=False)


@pytest.mark.parametrize("environment", ["dev", "uat", "prod"])
def test_helm_queue_matches_for_api_worker_and_beat(environment: str) -> None:
    root = Path(__file__).resolve().parents[1]
    for role in ("", "worker-", "beat-"):
        path = root / "helm" / f"values-{role}{environment}.yaml"
        values = yaml.safe_load(path.read_text())
        assert values["microservice-chart"]["envConfig"]["CELERY_QUEUE"] == (
            f"qa-centralhub-{environment}"
        )

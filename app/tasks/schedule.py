from __future__ import annotations

from app.tasks import celery_app

celery_app.conf.beat_schedule = {
    "sync-e2e-runs-hourly": {
        "task": "app.tasks.sync_e2e.sync_e2e_runs",
        "schedule": 3600.0,  # ogni ora
    },
}

from __future__ import annotations

from app.tasks import celery_app
from app.tasks import sync_psp_fee  # noqa: F401  (registers the task with celery_app)

celery_app.conf.beat_schedule = {
    "sync-e2e-runs-hourly": {
        "task": "app.tasks.sync_e2e.sync_e2e_runs",
        "schedule": 3600.0,  # ogni ora
    },
    "sync-psp-fee-services-daily": {
        "task": "app.tasks.sync_psp_fee.sync_psp_fee_services",
        "schedule": 86400.0,  # ogni 24h
    },
}

from __future__ import annotations

from celery.schedules import crontab

from app.tasks import (
    celery_app,
    sync_ea,  # noqa: F401  (registers the task with celery_app)
    sync_gpd_position,  # noqa: F401  (registers the task with celery_app)
    sync_psp_fee,  # noqa: F401  (registers the task with celery_app)
    sync_sanp_health,  # noqa: F401  (registers the task with celery_app)
)

celery_app.conf.beat_schedule = {
    "sync-psp-fee-services-daily": {
        "task": "app.tasks.sync_psp_fee.sync_psp_fee_services",
        "schedule": 86400.0,  # ogni 24h
    },
    "sync-gpd-position-daily": {
        "task": "app.tasks.sync_gpd_position.sync_gpd_position_snapshots",
        "schedule": 86400.0,  # ogni 24h
    },
    "sync-sanp-health-hourly": {
        "task": "app.tasks.sync_sanp_health.sync_sanp_health",
        "schedule": 3600.0,
    },
    "sync-ea-map-weekly": {
        "task": "app.tasks.sync_ea.sync_ea",
        "schedule": crontab(minute=17, hour=4, day_of_week="mon"),  # lunedì 04:17 UTC
    },
}

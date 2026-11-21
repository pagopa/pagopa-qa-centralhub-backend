from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from celery.schedules import crontab

from app.config import settings
from app.tasks import (
    celery_app,
    recompute_flaky_scores,  # noqa: F401  (registers the task with celery_app)
    sync_ea,  # noqa: F401  (registers the task with celery_app)
    sync_gpd_position,  # noqa: F401  (registers the task with celery_app)
    sync_psp_fee,  # noqa: F401  (registers the task with celery_app)
    sync_sanp_health,  # noqa: F401  (registers the task with celery_app)
    sync_status_page,  # noqa: F401  (registers the task with celery_app)
    sync_test_lifecycle,  # noqa: F401  (registers the task with celery_app)
    sync_test_lifecycle_jira,  # noqa: F401
    sync_test_lifecycle_nrt,  # noqa: F401
)

ROME = ZoneInfo("Europe/Rome")


def _now_rome() -> datetime:
    """Evaluate the flaky recompute crontab in Italian time without changing Celery's timezone."""
    return datetime.now(ROME)


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
    "sync-status-page": {
        "task": "app.tasks.sync_status_page.sync_status_page",
        "schedule": settings.status_page_sync_interval_s,  # every 15 minutes
        "options": {"expires": settings.status_page_sync_interval_s},
    },
    "sync-test-lifecycle-git": {
        "task": "app.tasks.sync_test_lifecycle.sync_test_lifecycle_git",
        "schedule": settings.tl_sync_interval_s,
        "options": {"expires": settings.tl_sync_interval_s},
    },
    "refresh-test-lifecycle-jira": {
        "task": "app.tasks.sync_test_lifecycle_jira.refresh_jira",
        "schedule": settings.tl_jira_refresh_interval_s,
        "options": {"expires": settings.tl_jira_refresh_interval_s},
    },
    "promote-test-lifecycle-running": {
        "task": "app.tasks.sync_test_lifecycle_nrt.promote_running",
        "schedule": settings.tl_nrt_running_interval_s,
        "options": {"expires": settings.tl_nrt_running_interval_s},
    },
    "sync-ea-map-weekly": {
        "task": "app.tasks.sync_ea.sync_ea",
        "schedule": crontab(minute=17, hour=4, day_of_week="mon"),  # lunedì 04:17 UTC
    },
    "recompute-flaky-scores-daily": {
        "task": "app.tasks.recompute_flaky_scores.recompute_flaky_scores",
        "schedule": crontab(minute=0, hour=12, nowfun=_now_rome),  # 12:00 Europe/Rome
    },
}

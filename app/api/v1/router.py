from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    bdd,
    coverage,
    dashboards,
    docs,
    dq,
    e2e,
    gpd_position,
    health,
    integrations,
    jira,
    notifications,
    overview,
    perf,
    psp_fee,
    releases,
    roles,
    runs,
    tm,
    users,
)

router = APIRouter()

router.include_router(health.router, tags=["health"])
router.include_router(bdd.router, prefix="/bdd", tags=["bdd"])
router.include_router(overview.router, prefix="/overview", tags=["overview"])
router.include_router(runs.router, prefix="/runs", tags=["runs"])
router.include_router(e2e.router, prefix="/e2e", tags=["e2e"])
router.include_router(psp_fee.router, prefix="/psp-fees", tags=["psp-fees"])
router.include_router(gpd_position.router, prefix="/gpd-position", tags=["gpd-position"])
router.include_router(coverage.router, prefix="/coverage", tags=["coverage"])
router.include_router(jira.router, prefix="/jira", tags=["jira"])
router.include_router(releases.router, prefix="/releases", tags=["releases"])
router.include_router(docs.router, prefix="/docs", tags=["docs"])
router.include_router(dq.router, prefix="/dq", tags=["dq"])
router.include_router(perf.router, prefix="/perf", tags=["perf"])
router.include_router(dashboards.router, prefix="/dashboards", tags=["dashboards"])
router.include_router(integrations.router, prefix="/integrations", tags=["integrations"])
router.include_router(users.router, prefix="/users", tags=["users"])
router.include_router(roles.router, prefix="/roles", tags=["roles"])
router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
router.include_router(tm.router, prefix="/tm", tags=["tm"])

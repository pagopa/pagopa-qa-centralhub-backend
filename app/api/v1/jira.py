from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.schemas.jira import JiraOverview, JiraTrend
from app.services.jira import BOARD_TESTING, JiraClient, compute_overview, compute_trend

router = APIRouter()

BASE_JQL_TESTING = 'project = PQ AND labels = "testing"'
BASE_JQL_SANP = 'project = PIDM AND "request type" = "SANP/SACI Support (PIDM)"'
BASE_JQL_DATA = 'project = PIDM AND "request type" = "Data Quality Support (PIDM)"'


async def _require_client() -> JiraClient:
    from app.config import settings
    if not settings.jira_api_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Jira not configured")
    return JiraClient()


# ── Testing ───────────────────────────────────────────────────────────────────

@router.get("/overview", response_model=JiraOverview)
async def get_overview() -> JiraOverview:
    client = await _require_client()
    issues = await client.get_board_issues(BOARD_TESTING)
    return JiraOverview(**compute_overview(issues))


@router.get("/trend", response_model=JiraTrend)
async def get_trend() -> JiraTrend:
    client = await _require_client()
    issues = await client.get_board_issues(BOARD_TESTING)
    return JiraTrend(weeks=compute_trend(issues, weeks=12))


# ── Supporto SANP / SACI ──────────────────────────────────────────────────────

@router.get("/sanp/overview", response_model=JiraOverview)
async def get_sanp_overview() -> JiraOverview:
    client = await _require_client()
    issues = await client.get_issues_by_jql(BASE_JQL_SANP)
    return JiraOverview(**compute_overview(issues))


@router.get("/sanp/trend", response_model=JiraTrend)
async def get_sanp_trend() -> JiraTrend:
    client = await _require_client()
    issues = await client.get_issues_by_jql(BASE_JQL_SANP)
    return JiraTrend(weeks=compute_trend(issues, weeks=12))


# ── Supporto Data ─────────────────────────────────────────────────────────────

@router.get("/data/overview", response_model=JiraOverview)
async def get_data_overview() -> JiraOverview:
    client = await _require_client()
    issues = await client.get_issues_by_jql(BASE_JQL_DATA)
    return JiraOverview(**compute_overview(issues))


@router.get("/data/trend", response_model=JiraTrend)
async def get_data_trend() -> JiraTrend:
    client = await _require_client()
    issues = await client.get_issues_by_jql(BASE_JQL_DATA)
    return JiraTrend(weeks=compute_trend(issues, weeks=12))

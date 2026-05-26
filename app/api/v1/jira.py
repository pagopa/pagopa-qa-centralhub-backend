from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.schemas.jira import JiraOverview, JiraTrend
from app.services.jira import BOARD_TESTING, JiraClient, compute_overview, compute_trend

router = APIRouter()


async def _get_issues() -> list[dict]:
    from app.config import settings
    if not settings.jira_api_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Jira not configured")
    client = JiraClient()
    return await client.get_board_issues(BOARD_TESTING)


@router.get("/overview", response_model=JiraOverview)
async def get_overview() -> JiraOverview:
    issues = await _get_issues()
    data = compute_overview(issues)
    return JiraOverview(**data)


@router.get("/trend", response_model=JiraTrend)
async def get_trend() -> JiraTrend:
    issues = await _get_issues()
    weeks = compute_trend(issues, weeks=12)
    return JiraTrend(weeks=weeks)

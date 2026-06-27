from __future__ import annotations

from pydantic import BaseModel


class NameCount(BaseModel):
    name: str
    count: int


class TypeCount(BaseModel):
    name: str
    count: int
    phase: str


class JiraAlert(BaseModel):
    key: str
    summary: str
    status: str
    days: int


class JiraOverview(BaseModel):
    total: int
    by_status: list[NameCount]
    by_component: list[NameCount]
    by_type: list[TypeCount]
    by_assignee: list[NameCount]
    alerts_no_estimate: list[JiraAlert]
    alerts_backlog_old: list[JiraAlert]
    alerts_blocked_old: list[JiraAlert]
    alerts_open_old: list[JiraAlert]
    alerts_in_progress_old: list[JiraAlert]


class TrendWeek(BaseModel):
    week: str
    label: str
    created: int
    closed: int


class JiraTrend(BaseModel):
    weeks: list[TrendWeek]


class EstimateDriftGroup(BaseModel):
    name: str
    original_estimate_sec: int
    time_spent_sec: int


class EstimateDriftItem(BaseModel):
    key: str
    summary: str
    issue_type: str
    assignee: str
    original_estimate_sec: int
    time_spent_sec: int
    drift_sec: int
    drift_pct: float


class JiraEstimateDrift(BaseModel):
    issues_with_estimate: int
    total_original_sec: int
    total_spent_sec: int
    drift_sec: int
    by_assignee: list[EstimateDriftGroup]
    by_type: list[EstimateDriftGroup]
    items: list[EstimateDriftItem]

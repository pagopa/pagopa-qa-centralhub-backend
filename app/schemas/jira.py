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


class TrendWeek(BaseModel):
    week: str
    discovery: int
    delivery: int
    support: int
    other: int
    done: int


class JiraTrend(BaseModel):
    weeks: list[TrendWeek]

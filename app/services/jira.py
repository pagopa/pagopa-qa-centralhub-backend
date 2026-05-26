from __future__ import annotations

from datetime import datetime, timezone, timedelta

import httpx

from app.config import settings

BOARD_TESTING = 597

DISCOVERY_TYPES = {
    "Comprensione del requisito",
    "Comprensione requisito",
    "Progettazione Scenari e casi di test",
}

DELIVERY_TYPES = {
    "Implementazione suite di TA",
    "Implementazione suite di TM",
    "Esecuzione suite di TA",
    "Esecuzione suite di TM",
}

SUPPORT_TYPES = {
    "Technical",
    "Bug fix validation",
    "Build Run Analysis",
    "Analisi e progettazione della deliverable di QA",
    "Implementazione e collaudo della deliverable di QA",
    "KT task",
}


def _phase(issue_type: str) -> str:
    if issue_type in DISCOVERY_TYPES:
        return "discovery"
    if issue_type in DELIVERY_TYPES:
        return "delivery"
    if issue_type in SUPPORT_TYPES:
        return "support"
    return "other"


class JiraClient:
    def __init__(self) -> None:
        self.base = settings.jira_base_url.rstrip("/")
        self.auth = (settings.jira_email, settings.jira_api_token)

    async def get_board_issues(self, board_id: int) -> list[dict]:
        issues: list[dict] = []
        start = 0
        fields = (
            "summary,status,issuetype,priority,assignee,"
            "created,updated,components,timeoriginalestimate,resolutiondate"
        )
        async with httpx.AsyncClient(timeout=30) as c:
            while True:
                r = await c.get(
                    f"{self.base}/rest/agile/1.0/board/{board_id}/issue",
                    auth=self.auth,
                    params={"startAt": start, "maxResults": 100, "fields": fields},
                )
                r.raise_for_status()
                data = r.json()
                batch = data.get("issues", [])
                issues.extend(batch)
                if start + len(batch) >= data.get("total", 0):
                    break
                start += len(batch)
        return issues


def compute_overview(issues: list[dict]) -> dict:
    now = datetime.now(timezone.utc)

    by_status: dict[str, int] = {}
    by_component: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_assignee: dict[str, int] = {}
    alerts_no_estimate: list[dict] = []
    alerts_backlog_old: list[dict] = []
    alerts_blocked_old: list[dict] = []

    for issue in issues:
        f = issue["fields"]
        key = issue["key"]
        summary = f["summary"]
        status = f["status"]["name"]
        itype = f["issuetype"]["name"]
        created = datetime.fromisoformat(f["created"].replace("Z", "+00:00"))
        updated = datetime.fromisoformat(f["updated"].replace("Z", "+00:00"))
        age_days = (now - created).days
        last_update_days = (now - updated).days

        by_status[status] = by_status.get(status, 0) + 1

        itype_display = itype if itype != "Epic" else "Epic"
        by_type[itype_display] = by_type.get(itype_display, 0) + 1

        for comp in f.get("components") or []:
            name = comp["name"]
            by_component[name] = by_component.get(name, 0) + 1

        assignee = f.get("assignee")
        if assignee:
            aname = assignee["displayName"]
            by_assignee[aname] = by_assignee.get(aname, 0) + 1

        if status == "In Progress" and not f.get("timeoriginalestimate"):
            alerts_no_estimate.append({"key": key, "summary": summary, "status": status, "days": age_days})

        if status == "Backlog" and age_days >= 30:
            alerts_backlog_old.append({"key": key, "summary": summary, "status": status, "days": age_days})

        if status == "BLOCKED" and last_update_days >= 30:
            alerts_blocked_old.append({"key": key, "summary": summary, "status": status, "days": last_update_days})

    status_order = ["Backlog", "Selected for Development", "In Progress", "READY FOR REVIEW", "IN REVIEW", "BLOCKED", "WAITING FOR", "Done"]
    by_status_sorted = sorted(by_status.items(), key=lambda x: (status_order.index(x[0]) if x[0] in status_order else 99, x[0]))

    return {
        "total": len(issues),
        "by_status": [{"name": k, "count": v} for k, v in by_status_sorted],
        "by_component": [{"name": k, "count": v} for k, v in sorted(by_component.items(), key=lambda x: -x[1])],
        "by_type": [{"name": k, "count": v, "phase": _phase(k)} for k, v in sorted(by_type.items(), key=lambda x: -x[1])],
        "by_assignee": [{"name": k, "count": v} for k, v in sorted(by_assignee.items(), key=lambda x: -x[1])],
        "alerts_no_estimate": sorted(alerts_no_estimate, key=lambda x: -x["days"]),
        "alerts_backlog_old": sorted(alerts_backlog_old, key=lambda x: -x["days"]),
        "alerts_blocked_old": sorted(alerts_blocked_old, key=lambda x: -x["days"]),
    }


def compute_trend(issues: list[dict], weeks: int = 12) -> list[dict]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=weeks)

    # Build week buckets: keyed by ISO week string "YYYY-Www"
    buckets: dict[str, dict] = {}
    for i in range(weeks):
        d = now - timedelta(weeks=weeks - 1 - i)
        key = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        buckets[key] = {"week": key, "discovery": 0, "delivery": 0, "support": 0, "other": 0, "done": 0}

    for issue in issues:
        f = issue["fields"]
        created = datetime.fromisoformat(f["created"].replace("Z", "+00:00"))
        if created < cutoff:
            continue
        wkey = f"{created.isocalendar()[0]}-W{created.isocalendar()[1]:02d}"
        if wkey not in buckets:
            continue
        itype = f["issuetype"]["name"]
        phase = _phase(itype)
        if f["status"]["name"] == "Done":
            buckets[wkey]["done"] += 1
        else:
            buckets[wkey][phase] += 1

    return list(buckets.values())

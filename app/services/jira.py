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

    async def get_issues_by_jql(self, jql: str) -> list[dict]:
        issues: list[dict] = []
        fields = [
            "summary", "status", "issuetype", "priority", "assignee",
            "created", "updated", "components", "timeoriginalestimate", "timespent", "resolutiondate",
        ]
        next_page_token: str | None = None
        async with httpx.AsyncClient(timeout=30) as c:
            while True:
                payload: dict = {"jql": jql, "maxResults": 100, "fields": fields}
                if next_page_token:
                    payload["nextPageToken"] = next_page_token
                r = await c.post(
                    f"{self.base}/rest/api/3/search/jql",
                    auth=self.auth,
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
                issues.extend(data.get("issues", []))
                if data.get("isLast", True):
                    break
                next_page_token = data.get("nextPageToken")
                if not next_page_token:
                    break
        return issues

    async def get_board_issues(self, board_id: int) -> list[dict]:
        issues: list[dict] = []
        start = 0
        fields = (
            "summary,status,issuetype,priority,assignee,"
            "created,updated,components,timeoriginalestimate,timespent,resolutiondate"
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
    alerts_open_old: list[dict] = []
    alerts_in_progress_old: list[dict] = []

    OPEN_STATUSES = {"Open", "Pending", "Nuovo", "New", "Waiting for Support", "Waiting for Customer"}
    IN_PROGRESS_STATUSES = {"In Progress", "In Review", "In lavorazione", "In Revisione", "READY FOR REVIEW", "IN REVIEW"}

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

        if status != "Done":
            assignee = f.get("assignee")
            if assignee:
                aname = assignee["displayName"]
                by_assignee[aname] = by_assignee.get(aname, 0) + 1

        if status == "In Progress" and not f.get("timeoriginalestimate") and itype != "Epic":
            alerts_no_estimate.append({"key": key, "summary": summary, "status": status, "days": age_days})

        if status == "Backlog" and age_days >= 30:
            alerts_backlog_old.append({"key": key, "summary": summary, "status": status, "days": age_days})

        if status == "BLOCKED" and last_update_days >= 30:
            alerts_blocked_old.append({"key": key, "summary": summary, "status": status, "days": last_update_days})

        if status in OPEN_STATUSES and last_update_days > 5:
            alerts_open_old.append({"key": key, "summary": summary, "status": status, "days": last_update_days})

        if status in IN_PROGRESS_STATUSES and last_update_days > 10:
            alerts_in_progress_old.append({"key": key, "summary": summary, "status": status, "days": last_update_days})

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
        "alerts_open_old": sorted(alerts_open_old, key=lambda x: -x["days"]),
        "alerts_in_progress_old": sorted(alerts_in_progress_old, key=lambda x: -x["days"]),
    }


def _week_label(iso_key: str) -> str:
    """Convert 'YYYY-Www' to a readable label like '12 mag'."""
    year, wnum = iso_key.split("-W")
    # ISO week Monday
    d = datetime.fromisocalendar(int(year), int(wnum), 1)
    months = ["gen", "feb", "mar", "apr", "mag", "giu",
              "lug", "ago", "set", "ott", "nov", "dic"]
    return f"{d.day} {months[d.month - 1]}"


def compute_estimate_drift(issues: list[dict]) -> dict:
    """Compute drift between original estimate and time spent for issues with an estimate."""
    items: list[dict] = []
    by_assignee: dict[str, dict] = {}
    by_type: dict[str, dict] = {}

    for issue in issues:
        f = issue["fields"]
        original = f.get("timeoriginalestimate") or 0
        if original == 0:
            continue  # only issues with a stima originaria

        spent = f.get("timespent") or 0
        drift = spent - original
        drift_pct = round((drift / original) * 100, 1)

        key = issue["key"]
        summary = f["summary"]
        itype = f["issuetype"]["name"]
        assignee_info = f.get("assignee")
        assignee = assignee_info["displayName"] if assignee_info else "Unassigned"

        items.append({
            "key": key,
            "summary": summary,
            "issue_type": itype,
            "assignee": assignee,
            "original_estimate_sec": original,
            "time_spent_sec": spent,
            "drift_sec": drift,
            "drift_pct": drift_pct,
        })

        if assignee not in by_assignee:
            by_assignee[assignee] = {"name": assignee, "original_estimate_sec": 0, "time_spent_sec": 0}
        by_assignee[assignee]["original_estimate_sec"] += original
        by_assignee[assignee]["time_spent_sec"] += spent

        if itype not in by_type:
            by_type[itype] = {"name": itype, "original_estimate_sec": 0, "time_spent_sec": 0}
        by_type[itype]["original_estimate_sec"] += original
        by_type[itype]["time_spent_sec"] += spent

    total_original = sum(i["original_estimate_sec"] for i in items)
    total_spent = sum(i["time_spent_sec"] for i in items)

    return {
        "issues_with_estimate": len(items),
        "total_original_sec": total_original,
        "total_spent_sec": total_spent,
        "drift_sec": total_spent - total_original,
        "by_assignee": sorted(
            by_assignee.values(),
            key=lambda x: x["time_spent_sec"] - x["original_estimate_sec"],
            reverse=True,
        ),
        "by_type": sorted(
            by_type.values(),
            key=lambda x: x["time_spent_sec"] - x["original_estimate_sec"],
            reverse=True,
        ),
        "items": sorted(items, key=lambda x: x["drift_sec"], reverse=True),
    }


def compute_trend(issues: list[dict], weeks: int = 12) -> list[dict]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=weeks)

    buckets: dict[str, dict] = {}
    for i in range(weeks):
        d = now - timedelta(weeks=weeks - 1 - i)
        key = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        buckets[key] = {"week": key, "label": _week_label(key), "created": 0, "closed": 0}

    for issue in issues:
        f = issue["fields"]

        created = datetime.fromisoformat(f["created"].replace("Z", "+00:00"))
        if created >= cutoff:
            wkey = f"{created.isocalendar()[0]}-W{created.isocalendar()[1]:02d}"
            if wkey in buckets:
                buckets[wkey]["created"] += 1

        resolution = f.get("resolutiondate")
        if resolution:
            closed = datetime.fromisoformat(resolution.replace("Z", "+00:00"))
            if closed >= cutoff:
                wkey = f"{closed.isocalendar()[0]}-W{closed.isocalendar()[1]:02d}"
                if wkey in buckets:
                    buckets[wkey]["closed"] += 1

    return list(buckets.values())

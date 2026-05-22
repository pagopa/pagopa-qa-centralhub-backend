from __future__ import annotations

# Permission matrix — mirrors frontend lib/permissions.ts
CAN: dict[str, set[str]] = {
    "qa_engineer": {
        "view:overview", "view:e2e", "view:coverage", "view:perf",
        "view:jira", "view:bugs", "view:releases", "view:docs",
        "view:dashboards",
        "ingest:runs",
    },
    "qa_lead": {
        # all qa_engineer permissions
        "view:overview", "view:e2e", "view:coverage", "view:perf",
        "view:jira", "view:bugs", "view:releases", "view:docs",
        "view:dashboards",
        "ingest:runs",
        # plus admin
        "view:settings",
        "manage:integrations", "manage:team", "manage:notifications",
        "manage:dashboards",
    },
    "product_owner": {
        "view:overview", "view:dashboards", "view:releases",
    },
    "stakeholder": {
        "view:overview", "view:dashboards",
    },
}


def can(role: str, action: str) -> bool:
    return action in CAN.get(role, set())

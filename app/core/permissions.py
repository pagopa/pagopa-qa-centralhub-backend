from __future__ import annotations

from typing import TypedDict

from app.models.role import Role


class CatalogEntry(TypedDict):
    key: str
    label: str
    category: str
    defaults: dict[str, bool]


ACTION_CATALOG: list[CatalogEntry] = [
    {
        "key": "view:overview",
        "label": "Overview",
        "category": "Generale",
        "defaults": {"qa_manager": True, "qa_analyst": True, "qa_engineer": True, "guest": True},
    },
    {
        "key": "view:bdd",
        "label": "Gherkin Generator (visualizza)",
        "category": "BDD",
        "defaults": {"qa_manager": True, "qa_analyst": True, "qa_engineer": True, "guest": False},
    },
    {
        "key": "manage:bdd",
        "label": "Gherkin Generator (crea/modifica + impostazioni)",
        "category": "BDD",
        "defaults": {"qa_manager": True, "qa_analyst": False, "qa_engineer": True, "guest": False},
    },
    {
        "key": "view:e2e",
        "label": "E2E Test Results",
        "category": "Test Results",
        "defaults": {"qa_manager": True, "qa_analyst": True, "qa_engineer": True, "guest": False},
    },
    {
        "key": "view:jira",
        "label": "KPI Jira",
        "category": "Project Tracking",
        "defaults": {"qa_manager": True, "qa_analyst": True, "qa_engineer": True, "guest": False},
    },
    {
        "key": "view:data_hub",
        "label": "Data Hub (PSP Commissioni, Posizioni GPD)",
        "category": "Data Hub",
        "defaults": {"qa_manager": True, "qa_analyst": True, "qa_engineer": True, "guest": False},
    },
    {
        "key": "view:docs",
        "label": "Docs & Decks",
        "category": "Knowledge Base",
        "defaults": {"qa_manager": True, "qa_analyst": True, "qa_engineer": True, "guest": True},
    },
    {
        "key": "manage:integrations",
        "label": "Settings: Integrazioni",
        "category": "Amministrazione",
        "defaults": {"qa_manager": True, "qa_analyst": False, "qa_engineer": False, "guest": False},
    },
    {
        "key": "sync:trigger",
        "label": "Azioni di Sync (↻)",
        "category": "Generale",
        "defaults": {"qa_manager": True, "qa_analyst": True, "qa_engineer": True, "guest": False},
    },
    {
        "key": "view:api_docs",
        "label": "API Docs (Swagger)",
        "category": "Amministrazione",
        "defaults": {"qa_manager": True, "qa_analyst": True, "qa_engineer": True, "guest": False},
    },
    {
        "key": "view:data_quality",
        "label": "Data Quality (visualizza)",
        "category": "Data Quality",
        "defaults": {"qa_manager": True, "qa_analyst": True, "qa_engineer": True, "guest": False},
    },
    {
        "key": "manage:data_quality",
        "label": "Data Quality (crea/modifica controlli e istanze)",
        "category": "Data Quality",
        "defaults": {"qa_manager": True, "qa_analyst": False, "qa_engineer": True, "guest": False},
    },
]

ACTION_KEYS: set[str] = {entry["key"] for entry in ACTION_CATALOG}

EDITABLE_ROLES = ("qa_manager", "qa_analyst", "qa_engineer", "guest")


def compute_role_matrix(roles: list[Role]) -> dict[str, dict[str, bool]]:
    matrix: dict[str, dict[str, bool]] = {}
    for role in roles:
        if role.is_system:
            matrix[role.key] = {entry["key"]: True for entry in ACTION_CATALOG}
        else:
            matrix[role.key] = {
                entry["key"]: role.permissions.get(entry["key"], entry["defaults"][role.key])
                for entry in ACTION_CATALOG
            }
    return matrix

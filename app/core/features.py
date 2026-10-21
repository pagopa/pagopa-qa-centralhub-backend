from __future__ import annotations

from typing import TypedDict


class FeatureDefinition(TypedDict):
    key: str
    label: str
    route: str
    action: str
    external: bool


FEATURE_CATALOG: tuple[FeatureDefinition, ...] = (
    {
        "key": "overview",
        "label": "Overview",
        "route": "/",
        "action": "view:overview",
        "external": False,
    },
    {
        "key": "bdd",
        "label": "Gherkin Generator",
        "route": "/bdd",
        "action": "view:bdd",
        "external": False,
    },
    {
        "key": "settings_gherkin",
        "label": "Settings Gherkin",
        "route": "/settings/bdd",
        "action": "manage:bdd",
        "external": False,
    },
    {
        "key": "settings_e2e",
        "label": "Settings E2E",
        "route": "/settings/integrations",
        "action": "manage:integrations",
        "external": False,
    },
    {"key": "e2e", "label": "E2E", "route": "/e2e", "action": "view:e2e", "external": False},
    {
        "key": "sanp_health",
        "label": "SANP Health",
        "route": "/sanp-health",
        "action": "view:sanp_health",
        "external": False,
    },
    {
        "key": "nrt_metrics",
        "label": "NRT Metrics",
        "route": "/nrt-metrics",
        "action": "view:nrt_metrics",
        "external": False,
    },
    {
        "key": "jira",
        "label": "KPI Jira",
        "route": "/jira",
        "action": "view:jira",
        "external": False,
    },
    {
        "key": "psp_fees",
        "label": "PSP Commissioni",
        "route": "/data-hub/psp-fees",
        "action": "view:data_hub",
        "external": False,
    },
    {
        "key": "gpd_positions",
        "label": "Posizioni Debitorie GPD",
        "route": "/data-hub/gpd-positions",
        "action": "view:data_hub",
        "external": False,
    },
    {
        "key": "dq_catalog",
        "label": "Catalogo Controlli",
        "route": "/data-quality/catalog",
        "action": "view:data_quality",
        "external": False,
    },
    {
        "key": "dq_domains",
        "label": "Controlli per Dominio",
        "route": "/data-quality/domains",
        "action": "view:data_quality",
        "external": False,
    },
    {
        "key": "ea_map",
        "label": "EA Map",
        "route": "/ea-map",
        "action": "view:ea_map",
        "external": False,
    },
    {
        "key": "docs",
        "label": "Docs & Decks",
        "route": "/docs",
        "action": "view:docs",
        "external": False,
    },
    {
        "key": "api_docs",
        "label": "API Docs",
        "route": "/api/v1/docs",
        "action": "view:api_docs",
        "external": True,
    },
    {
        "key": "nrts",
        "label": "Non-Regression Tests (NRT)",
        "route": "/nrts",
        "action": "view:nrts",
        "external": False,
    },
    {
        "key": "tas_nrts",
        "label": "Test automation service (TAS)",
        "route": "/tas-nrts",
        "action": "view:tas_nrts",
        "external": False,
    },
)

FEATURE_KEYS = frozenset(feature["key"] for feature in FEATURE_CATALOG)

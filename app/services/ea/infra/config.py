# ruff: noqa: E501  (regex tables)
"""Configuration of the infrastructure view: sources and Terraform → logical type mapping.

Pure data, no logic: the same tables can be reused elsewhere (e.g. Atlas). The icon of each
logical type lives in the frontend (``infraIcons.ts``), keyed by ``ltype`` and ``vendor``.
See docs/ea-map/DESIGN-infra.md.
"""

from __future__ import annotations

# Repositories of the payments platform read by the sync (organisation: GITHUB_ORG).
INFRA_REPOS: tuple[str, ...] = ("pagopa-infra", "pagopa-infra-core", "pagopa-aws-infra")

# Snapshot key of an infra repository in ea_repo_snapshots (kept apart from components).
SNAPSHOT_PREFIX = "infra:"

# Environments shown by the selector, and how a stack's env folder maps onto them
# ("weu-prod", "itn-uat", "prod" ...).
ENVS: tuple[str, ...] = ("prod", "uat", "dev")
ENV_SHORT = {"prod": "p", "uat": "u", "dev": "d"}

# (logical type, vendor, regex on "<kind>:<tf type or module>"), first match wins.
# kind is resource | module | data; module is the library path (e.g. "IDH/subnet").
TYPE_RULES: list[tuple[str, str, str]] = [
    ("cdn", "azure", r"^(resource|data):azurerm_(cdn|frontdoor)|^module:cdn(_frontdoor)?$"),
    ("api_gateway", "azure", r"^(resource|data):azurerm_api_management|^module:api_management"),
    (
        "kubernetes",
        "azure",
        r"^(resource|data):azurerm_kubernetes|^resource:(kubernetes_namespace|helm_release)$|^module:(kubernetes_cluster|IDH/aks_node_pool)$",
    ),
    (
        "function",
        "azure",
        r"^(resource|data):azurerm_(linux_|windows_)?function_app|^module:(function_app|IDH/function_app)",
    ),
    (
        "app_service",
        "azure",
        r"^(resource|data):azurerm_((linux_|windows_)?web_app|service_plan|app_service_plan|static_web_app)|^module:(app_service|web_app|IDH/app_service)",
    ),
    (
        "container",
        "azure",
        r"^(resource|data):azurerm_(container_app|container_registry)|^module:container_app",
    ),
    (
        "storage",
        "azure",
        r"^(resource|data):azurerm_storage_(account|container|queue|table|share)$|^module:(storage_account|IDH/storage_account)$",
    ),
    (
        "db_sql",
        "azure",
        r"^(resource|data):azurerm_postgresql_flexible_server$|^module:(postgres|IDH/postgres)",
    ),
    ("db_nosql", "azure", r"^(resource|data):azurerm_cosmosdb|^module:cosmosdb"),
    ("cache", "azure", r"^(resource|data):azurerm_redis|^module:(redis|IDH/redis)"),
    (
        "messaging",
        "azure",
        r"^(resource|data):azurerm_(eventhub|servicebus|eventgrid)|^module:(eventhub|servicebus)",
    ),
    ("data_pipeline", "azure", r"^(resource|data):azurerm_data_factory"),
    ("analytics", "azure", r"^(resource|data):azurerm_kusto"),
    ("secrets", "azure", r"^(resource|data):azurerm_key_vault$|^module:(key_vault|IDH/key_vault)$"),
    (
        "network",
        "azure",
        r"^(resource|data):azurerm_(virtual_network|subnet|private_endpoint|private_dns_zone$|dns_zone$|public_ip|nat_gateway|application_gateway)|^module:(subnet|IDH/subnet|virtual_network|app_gw|nat_gateway|virtual_network_peering)$",
    ),
    (
        "identity",
        "azure",
        r"^(resource|data):azurerm_user_assigned_identity|^module:(github_federated_identity|IDH/github_federated_identity)$",
    ),
    (
        "monitoring",
        "azure",
        r"^(resource|data):azurerm_(application_insights$|log_analytics_workspace$|portal_dashboard$|monitor_(scheduled_query_rules_alert|metric_alert|activity_log_alert))|^module:application_insights",
    ),
    ("action_group", "azure", r"^(resource|data):azurerm_monitor_action_group$"),
    (
        "dashboard",
        "grafana",
        r"^(resource|data):azurerm_dashboard_grafana|^module:grafana|^resource:grafana_",
    ),
    ("resource_group", "azure", r"^(resource|data):azurerm_resource_group$"),
    ("email", "aws", r"^module:(ses|terraform-aws-ses)|^resource:aws_ses"),
    ("storage", "aws", r"^resource:aws_s3_bucket$"),
    ("function", "aws", r"^resource:aws_lambda_function$"),
    ("db_nosql", "aws", r"^resource:aws_dynamodb_table$"),
    ("messaging", "aws", r"^resource:aws_(sns_topic|sqs_queue)$"),
    ("container", "aws", r"^resource:aws_ecr_repository$"),
    ("monitoring", "aws", r"^resource:aws_cloudwatch_(log_group|metric_alarm)$"),
]

# Front doors / CDNs that serve the web frontends (hostnames and static storage are read).
CDN_TYPES: tuple[str, ...] = (
    "cdn_frontdoor",
    "cdn",
    "azurerm_cdn_frontdoor_profile",
    "azurerm_cdn_profile",
)

# Alerts, among the monitoring types.
ALERT_TYPES: tuple[str, ...] = (
    "azurerm_monitor_scheduled_query_rules_alert",
    "azurerm_monitor_scheduled_query_rules_alert_v2",
    "azurerm_monitor_metric_alert",
    "azurerm_monitor_activity_log_alert",
)

# Library module → Azure resource type it creates: lets a `data "azurerm_x"` of another stack
# find a resource created through a module (matched on the evaluated `name` input).
MODULE_AZURE_TYPES: dict[str, str] = {
    "kubernetes_cluster": "azurerm_kubernetes_cluster",
    "api_management": "azurerm_api_management",
    "key_vault": "azurerm_key_vault",
    "IDH/key_vault": "azurerm_key_vault",
    "cosmosdb_account": "azurerm_cosmosdb_account",
    "storage_account": "azurerm_storage_account",
    "IDH/storage_account": "azurerm_storage_account",
    "subnet": "azurerm_subnet",
    "IDH/subnet": "azurerm_subnet",
    "virtual_network": "azurerm_virtual_network",
    "redis_cache": "azurerm_redis_cache",
    "IDH/redis": "azurerm_redis_cache",
    "eventhub": "azurerm_eventhub_namespace",
    "function_app": "azurerm_linux_function_app",
    "IDH/function_app": "azurerm_linux_function_app",
    "postgres_flexible_server": "azurerm_postgresql_flexible_server",
    "IDH/postgres_flexible_server": "azurerm_postgresql_flexible_server",
    "application_insights": "azurerm_application_insights",
    "grafana": "azurerm_dashboard_grafana",
    "api_management_product": "azurerm_api_management_product",
    "api_management_api": "azurerm_api_management_api",
    "app_service": "azurerm_linux_web_app",
    "IDH/app_service_webapp": "azurerm_linux_web_app",
    "cdn_frontdoor": "azurerm_cdn_frontdoor_profile",
}

# Azure types that name the same kind of resource: a data block of one finds the other.
TYPE_ALIASES: dict[str, str] = {
    "azurerm_function_app": "azurerm_linux_function_app",
    "azurerm_windows_function_app": "azurerm_linux_function_app",
    "azurerm_app_service": "azurerm_linux_web_app",
    "azurerm_windows_web_app": "azurerm_linux_web_app",
}

# Attributes that make the referenced resource the container of the block.
CONTAINER_ATTRS: tuple[str, ...] = (
    "subnet_id",
    "virtual_network_name",
    "kubernetes_cluster_id",
    "namespace",
    "resource_group_name",
)
# logical types that can contain other resources (drawn as boxes)
CONTAINER_LTYPES: tuple[str, ...] = ("resource_group", "network", "kubernetes")

# Helm releases / namespaces / function apps / APIM APIs whose name matches a map component
# are proposed as links resource → component (inferred, reviewed by a person).
LINKABLE_TF_TYPES: tuple[str, ...] = (
    "helm_release",
    "kubernetes_namespace",
    "function_app",
    "IDH/function_app",
    "api_management_api",
    "azurerm_api_management_api",
    "azurerm_linux_function_app",
    "azurerm_linux_web_app",
    "IDH/app_service_webapp",
)

# Resource (tf type or library module) → blueprint infra node of the map it belongs to, for
# the click-through between the blueprint and this view. "*aws": every AWS resource.
BLUEPRINT_CATEGORIES: dict[str, str] = {
    "api_management": "in-apim",
    "azurerm_api_management": "in-apim",
    "cdn_frontdoor": "in-cdn",
    "cdn": "in-cdn",
    "azurerm_cdn_frontdoor_profile": "in-cdn",
    "azurerm_cdn_profile": "in-cdn",
    "postgres_flexible_server": "in-pg",
    "IDH/postgres_flexible_server": "in-pg",
    "azurerm_postgresql_flexible_server": "in-pg",
    "cosmosdb_account": "in-cosmos",
    "azurerm_cosmosdb_account": "in-cosmos",
    "eventhub": "in-eh",
    "azurerm_eventhub_namespace": "in-eh",
    "azurerm_kusto_cluster": "in-adx",
    "storage_account": "in-storage",
    "IDH/storage_account": "in-storage",
    "azurerm_storage_account": "in-storage",
    "function_app": "in-fn",
    "IDH/function_app": "in-fn",
    "azurerm_linux_function_app": "in-fn",
    "*aws": "in-aws",
}

"""Infrastructure view of the EA Map (docs/ea-map/DESIGN-infra.md)."""

from __future__ import annotations

# ruff: noqa: S101
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.main import app
from app.models.ea import EaChange, EaInfraLink, EaInfraResource, EaRepoSnapshot
from app.services.ea.importer import import_kb
from app.services.ea.infra.parser import MASK, Scope, parse_repo
from app.services.ea.infra.resolve import build, component_key, name_variants
from app.services.ea.sync import review
from app.services.ea.sync.engine import create_run, execute_run
from tests._db import TestSession, get_test_db
from tests.test_ea_service import _wipe
from tests.test_ea_sync import NOW, PAY, FakeConfluence, FakeGitHub, _kb, _repo

CORE = "pagopa-infra-core"
APP = "pagopa-infra"

CORE_FILES = {
    "src/core/main.tf": """
locals {
  project = "${var.prefix}-${var.env_short}"
}
resource "azurerm_resource_group" "monitor_rg" {
  name     = "${local.project}-monitor-rg"
  location = var.location
}
resource "azurerm_application_insights" "appi" {
  name                = "${local.project}-appinsights"
  resource_group_name = azurerm_resource_group.monitor_rg.name
}
resource "azurerm_monitor_action_group" "slack" {
  name                = "SlackPagoPA"
  resource_group_name = azurerm_resource_group.monitor_rg.name
  short_name          = "Slack"
  email_receiver {
    name          = "sendtoslack"
    email_address = data.azurerm_key_vault_secret.slack_email.value
  }
}
resource "azurerm_monitor_action_group" "opsgenie" {
  name                = "Opsgenie"
  resource_group_name = azurerm_resource_group.monitor_rg.name
  short_name          = "Opsgenie"
  webhook_receiver {
    name        = "opsgenie"
    service_uri = "https://api.opsgenie.com/v1/json/azure"
  }
}
data "azurerm_key_vault_secret" "slack_email" {
  name = "monitor-notification-slack-email"
}
module "aks" {
  source              = "./.terraform/modules/__v4__//kubernetes_cluster"
  name                = "${local.project}-weu-aks"
  resource_group_name = azurerm_resource_group.monitor_rg.name
}
resource "azurerm_key_vault_secret" "noise" {
  name = "x"
}
""",
    "src/core/99_variables.tf": """
variable "prefix" {
  default = "pagopa"
}
""",
    "src/core/env/weu-prod/terraform.tfvars": """
env_short = "p"
location  = "westeurope"
vnet_cidr = ["10.1.0.0/16"]
""",
    "src/core/env/weu-dev/terraform.tfvars": """
env_short = "d"
location  = "westeurope"
""",
}

APP_FILES = {
    "src/domains/gps-app/main.tf": """
locals {
  project = "${var.prefix}-${var.env_short}"
}
data "azurerm_application_insights" "appi" {
  name = "${local.project}-appinsights"
}
data "azurerm_monitor_action_group" "slack" {
  name = "SlackPagoPA"
}
data "azurerm_kubernetes_cluster" "aks" {
  name = "${local.project}-weu-aks"
}
data "azurerm_client_config" "current" {}
resource "kubernetes_namespace" "namespace" {
  metadata {
    name = "gps"
  }
}
module "apim_gpd_payments_api" {
  source = "./.terraform/modules/__v4__/api_management_api"
  name   = "${local.project}-weu-gpd-payments-api"
}
module "apim_gpd_payments_mock" {
  source = "./.terraform/modules/__v4__/api_management_api"
  name   = "${local.project}-weu-gpd-payments-mock"
}
resource "azurerm_monitor_scheduled_query_rules_alert" "gpd_availability" {
  count          = var.env_short == "p" ? 1 : 0
  name           = "gpd-availability"
  data_source_id = data.azurerm_application_insights.appi.id
  severity       = 1
  frequency      = 5
  description    = "Sub 11111111-2222-3333-4444-555555555555 from 10.1.2.3"
  action {
    action_group = [data.azurerm_monitor_action_group.slack.id]
  }
}
resource "helm_release" "reloader" {
  name      = "reloader"
  chart     = "reloader"
  namespace = kubernetes_namespace.namespace.metadata[0].name
}
""",
    "src/domains/gps-app/99_variables.tf": """
variable "prefix" {
  default = "pagopa"
}
""",
    "src/domains/gps-app/env/weu-prod/terraform.tfvars": 'env_short = "p"\n',
    "src/domains/gps-app/env/weu-dev/terraform.tfvars": 'env_short = "d"\n',
}


def _res(extract: dict[str, Any], stack: str, addr: str) -> dict[str, Any]:
    return next(r for r in extract["stacks"][stack]["resources"] if r["addr"] == addr)


# ---- parser ------------------------------------------------------------------------------


def test_scope_evaluates_names_and_env_gating() -> None:
    sc = Scope(
        {"env_short": "p", "prefix": "pagopa"}, {"project": "${var.prefix}-${var.env_short}"}
    )
    assert sc.value("${local.project}-aks") == "pagopa-p-aks"
    assert sc.value('${format("%s-rg", local.project)}') == "pagopa-p-rg"
    assert sc.present('${var.env_short == "p" ? 1 : 0}') is True
    assert sc.present('${var.env_short != "p" ? 1 : 0}') is False
    assert sc.present("${var.unknown ? 1 : 0}") is True  # unknown: assume it exists
    assert sc.value("${data.x.y.name}") is None


def test_parse_repo_classifies_masks_and_keeps_no_tfvars() -> None:
    ext = parse_repo(APP_FILES)
    stack = ext["stacks"]["src/domains/gps-app"]
    assert stack["envs"] == ["prod", "dev"]
    alert = _res(
        ext, "src/domains/gps-app", "azurerm_monitor_scheduled_query_rules_alert.gpd_availability"
    )
    assert alert["ltype"] == "monitoring"
    assert alert["envs"] == ["prod"]  # count = env_short == "p"
    assert alert["attrs"]["severity"] == 1
    assert (
        "1111" not in alert["attrs"]["description"]
        and "10.1.2.3" not in alert["attrs"]["description"]
    )
    assert MASK in alert["attrs"]["description"]
    api = _res(ext, "src/domains/gps-app", "module.apim_gpd_payments_api")
    assert api["names"] == {
        "prod": "pagopa-p-weu-gpd-payments-api",
        "dev": "pagopa-d-weu-gpd-payments-api",
    }
    assert api["ltype"] == "api_gateway"
    refs = {(r["from"], r["to"], r["attr"]) for r in stack["refs"]}
    assert (
        "azurerm_monitor_scheduled_query_rules_alert.gpd_availability",
        "data.azurerm_monitor_action_group.slack",
        "action",
    ) in refs

    core = parse_repo(CORE_FILES)
    assert "10.1.0.0" not in str(core)  # tfvars values are never stored
    groups = {
        r["addr"]: r["attrs"]["channels"]
        for r in core["stacks"]["src/core"]["resources"]
        if r["ltype"] == "action_group"
    }
    assert groups == {
        "azurerm_monitor_action_group.slack": ["slack"],
        "azurerm_monitor_action_group.opsgenie": ["opsgenie"],
    }
    assert _res(core, "src/core", "module.aks")["ltype"] == "kubernetes"
    assert core["noise"] == 1  # the key vault secret


def test_network_details_only_with_the_flag() -> None:
    files = {
        "s/net.tf": (
            'resource "azurerm_subnet" "snet" {\n  name = "snet"\n'
            "  address_prefixes = var.cidr\n}\n"
        ),
        "s/env/prod/terraform.tfvars": 'cidr = "10.0.0.0/24"\n',
    }
    assert "address_prefixes" not in _res(parse_repo(files), "s", "azurerm_subnet.snet")["attrs"]
    with_flag = _res(parse_repo(files, network_details=True), "s", "azurerm_subnet.snet")
    assert with_flag["attrs"]["address_prefixes"] == ["10.0.0.0/24"]


def test_unparsable_file_is_reported_not_fatal() -> None:
    ext = parse_repo({"s/bad.tf": "resource {", **CORE_FILES})
    assert "s/bad.tf" in ext["errors"]
    assert "src/core" in ext["stacks"]


# ---- resolver ----------------------------------------------------------------------------


def test_name_variants_and_component_key() -> None:
    assert "apiconfigcache" in name_variants("pagopa-p-weu-apiconfig-apiconfig-cache-api")
    assert component_key("pagopa-api-config-cache") == "apiconfigcache"
    assert component_key("pagopa-checkout-auth-service") == "checkoutauth"


def test_build_matches_data_across_repositories_and_proposes_links() -> None:
    g = build(
        {CORE: parse_repo(CORE_FILES), APP: parse_repo(APP_FILES)},
        components={PAY},
        namespaces={"gps": "ns-gps"},
        blueprint={"ns-gps", "in-apim"},
    )
    alert = (
        f"{APP}:src/domains/gps-app:azurerm_monitor_scheduled_query_rules_alert.gpd_availability"
    )
    appi = f"{CORE}:src/core:azurerm_application_insights.appi"
    slack = f"{CORE}:src/core:azurerm_monitor_action_group.slack"
    rg = f"{CORE}:src/core:azurerm_resource_group.monitor_rg"
    links = {(lk["from_id"], lk["to_id"]): lk for lk in g.links.values()}
    assert links[(alert, appi)]["kind"] == "monitors"
    assert links[(alert, appi)]["origin"] == "inferred"  # matched by name in another repo
    assert links[(alert, appi)]["envs"] == ["prod"]
    assert links[(alert, slack)]["kind"] == "notifies"
    assert g.resources[appi]["parent_id"] == rg  # resource group is a container, not an arrow
    assert (appi, rg) not in links
    helm = f"{APP}:src/domains/gps-app:helm_release.reloader"
    ns = f"{APP}:src/domains/gps-app:kubernetes_namespace.namespace"
    assert g.resources[helm]["parent_id"] == ns
    proposals = {(p["resource_id"].rsplit(":", 1)[-1], p["node_id"]) for p in g.proposals}
    assert proposals == {
        ("kubernetes_namespace.namespace", "ns-gps"),
        ("module.apim_gpd_payments_api", PAY),  # the mock is not the service
    }
    assert g.stats["data_matched"] == 2  # app insights and action group (aks is unreferenced)
    # client config is not a component: no external resource for it
    assert not any("client_config" in rid for rid in g.resources)


def test_unmatched_data_becomes_one_shared_external_resource() -> None:
    g = build({APP: parse_repo(APP_FILES)})
    externals = [r for r in g.resources.values() if r["external"]]
    assert {r["tf_type"] for r in externals} == {
        "azurerm_application_insights",
        "azurerm_monitor_action_group",
    }


# ---- sync, review and API ----------------------------------------------------------------


class InfraGitHub(FakeGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.set_repo(CORE, CORE_FILES)
        self.set_repo(APP, APP_FILES)

    def set_repo(self, repo: str, files: dict[str, str], pushed: Any = None) -> None:
        self.trees[repo] = [*files, "README.md"]
        self.files[repo] = dict(files)
        self.listing = [r for r in self.listing if r.name != repo]
        self.listing.append(_repo(repo, pushed=pushed or NOW))


@pytest.fixture(autouse=True)
def _test_engine_for_api() -> Iterator[None]:
    app.dependency_overrides[get_db] = get_test_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        await _wipe(session)
        kb = _kb()
        kb["blueprint"] = {
            "infra": [
                {"id": "ns-gps", "name": "Namespace gps", "group": "aks"},
                {"id": "in-apim", "name": "API Management", "group": "edge"},
            ]
        }
        await import_kb(session, kb)
        yield session
        await session.rollback()
        await _wipe(session)


async def _sync(db: AsyncSession, gh: FakeGitHub) -> dict[str, Any]:
    run = await create_run(db, trigger="manual", actor="tester")
    return await execute_run(db, run.id, gh, FakeConfluence())


async def test_sync_fills_the_infra_view_in_the_same_run(db: AsyncSession) -> None:
    gh = InfraGitHub()
    summary = await _sync(db, gh)
    assert summary["status"] == "success"  # pagopa-aws-infra not visible: skipped, not partial
    assert summary["stats"]["infra_repos_read"] == 2
    assert summary["stats"]["infra_alerts"] == 1
    snaps = {s.repo for s in await db.scalars(select(EaRepoSnapshot))}
    assert {f"infra:{CORE}", f"infra:{APP}"} <= snaps

    resources = {r.id: r for r in await db.scalars(select(EaInfraResource))}
    aks = resources[f"{CORE}:src/core:module.aks"]
    assert aks.names == {"prod": "pagopa-p-weu-aks", "dev": "pagopa-d-weu-aks"}
    assert aks.status == "active"
    assert await db.scalar(select(EaInfraLink.id).limit(1)) is not None
    changes = list(await db.scalars(select(EaChange).where(EaChange.entity == "infra")))
    summaries = [c for c in changes if c.status == "auto_applied"]
    assert {c.target_id for c in summaries} == {f"infra:{CORE}", f"infra:{APP}"}
    pending = {c.target_id: c for c in changes if c.status == "pending"}
    api = f"{APP}:src/domains/gps-app:module.apim_gpd_payments_api"
    assert set(pending) == {
        f"{APP}:src/domains/gps-app:kubernetes_namespace.namespace>ns-gps",
        f"{api}>{PAY}",
    }

    # second run: nothing pushed, snapshots reused, no new proposals
    tree_calls = len(gh.tree_calls)
    await _sync(db, gh)
    assert CORE not in gh.tree_calls[tree_calls:]
    again = list(
        await db.scalars(
            select(EaChange).where(EaChange.entity == "infra", EaChange.status == "pending")
        )
    )
    assert len(again) == 2

    # approve the API → component link: click-through from the component
    await review.decide(db, pending[f"{api}>{PAY}"].id, approve=True, actor="qa-lead")
    await db.commit()
    linked = await db.get(EaInfraResource, api)
    await db.refresh(linked)
    assert linked.ea_node_id == PAY

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        prod = (await client.get("/api/v1/ea/infra", params={"env": "prod"})).json()
        dev = (await client.get("/api/v1/ea/infra", params={"env": "dev"})).json()
        node = (await client.get(f"/api/v1/ea/nodes/{PAY}")).json()
        detail = (await client.get(f"/api/v1/ea/infra/resources/{api}")).json()
        bad = await client.get("/api/v1/ea/infra", params={"env": "staging"})
    assert bad.status_code == 422
    assert [a["label"] for a in prod["monitoring"]["alerts"]] == ["gpd-availability"]
    assert dev["monitoring"]["alerts"] == []  # prod-only alert
    alert = prod["monitoring"]["alerts"][0]
    assert alert["targets"] == [f"{CORE}:src/core:azurerm_application_insights.appi"]
    assert alert["action_groups"] == [f"{CORE}:src/core:azurerm_monitor_action_group.slack"]
    channels = {g["label"]: g["channels"] for g in prod["monitoring"]["action_groups"]}
    assert channels["SlackPagoPA"] == ["slack"]
    assert any(lk["origin"] == "inferred" for lk in prod["links"])
    assert {r["id"] for r in node["infra"]} == {api}
    assert detail["node"] == PAY and detail["node_name"] == PAY
    assert detail["file_url"].endswith(f"{APP}/blob/HEAD/src/domains/gps-app/main.tf")


async def test_a_new_parser_rereads_unchanged_repositories(db: AsyncSession) -> None:
    gh = InfraGitHub()
    await _sync(db, gh)
    snap = await db.get(EaRepoSnapshot, f"infra:{CORE}")
    snap.extracted = {**snap.extracted, "parser": 1}  # written by an older parser
    await db.commit()
    calls = len(gh.tree_calls)
    await _sync(db, gh)
    assert CORE in gh.tree_calls[calls:]
    assert APP not in gh.tree_calls[calls:]  # up to date: reused


async def test_unreadable_repository_keeps_its_resources_and_makes_a_partial_run(
    db: AsyncSession,
) -> None:
    gh = InfraGitHub()
    await _sync(db, gh)
    gh.set_repo(CORE, {}, pushed=NOW + timedelta(hours=1))
    gh.fail.add(CORE)
    summary = await _sync(db, gh)
    assert summary["status"] == "partial"
    aks = await db.get(EaInfraResource, f"{CORE}:src/core:module.aks")
    await db.refresh(aks)
    assert aks.status == "active"  # last known picture kept


async def test_vanished_resources_are_removed_and_rejections_stick(db: AsyncSession) -> None:
    gh = InfraGitHub()
    await _sync(db, gh)
    ns_change = await db.scalar(
        select(EaChange).where(
            EaChange.entity == "infra", EaChange.target_id.like("%namespace>ns-gps")
        )
    )
    await review.decide(db, ns_change.id, approve=False, actor="qa-lead")
    await db.commit()

    files = dict(APP_FILES)
    files["src/domains/gps-app/main.tf"] = files["src/domains/gps-app/main.tf"].replace(
        'module "apim_gpd_payments_mock"', 'module "apim_gpd_payments_mock_renamed"'
    )
    gh.set_repo(APP, files, pushed=NOW + timedelta(hours=1))
    await _sync(db, gh)

    old = await db.get(EaInfraResource, f"{APP}:src/domains/gps-app:module.apim_gpd_payments_mock")
    await db.refresh(old)
    assert old.status == "removed"
    assert (
        await db.get(
            EaInfraResource, f"{APP}:src/domains/gps-app:module.apim_gpd_payments_mock_renamed"
        )
    ) is not None
    reopened = list(
        await db.scalars(
            select(EaChange).where(
                EaChange.target_id.like("%namespace>ns-gps"), EaChange.status == "pending"
            )
        )
    )
    assert reopened == []  # a rejected link is never proposed again


CDN_FILES = {
    "src/domains/checkout-app/cdn.tf": """
locals {
  project           = "${var.prefix}-${var.env_short}-weu-checkout"
  dns_zone_platform = "${var.env == "prod" ? "platform" : "${var.env}.platform"}"
  custom_domains = [
    {
      domain_name = "checkout.${local.dns_zone_platform}.pagopa.it"
    }
  ]
}
module "checkout_cdn_frontdoor" {
  source               = "./.terraform/modules/__v4__/cdn_frontdoor"
  cdn_prefix_name      = local.project
  storage_account_name = "${replace(local.project, "-", "")}sa"
  custom_domains       = local.custom_domains
}
""",
    "src/domains/checkout-app/99_variables.tf": 'variable "prefix" {\n  default = "pagopa"\n}\n',
    "src/domains/checkout-app/env/weu-prod/terraform.tfvars": 'env_short = "p"\nenv = "prod"\n',
    "src/domains/checkout-app/env/weu-uat/terraform.tfvars": 'env_short = "u"\nenv = "uat"\n',
}


def test_front_doors_show_their_public_hostname_and_link_to_the_frontend() -> None:
    ext = parse_repo(CDN_FILES)
    cdn = _res(ext, "src/domains/checkout-app", "module.checkout_cdn_frontdoor")
    assert cdn["ltype"] == "cdn"
    # nested interpolation inside a conditional, evaluated per environment
    assert cdn["attrs"]["hostnames"] == ["checkout.platform.pagopa.it"]
    assert cdn["names"]["uat"] == "pagopa-u-weu-checkout"
    g = build({APP: ext}, components={"pagopa-checkout-fe", PAY}, frontends={"pagopa-checkout-fe"})
    res = g.resources[f"{APP}:src/domains/checkout-app:module.checkout_cdn_frontdoor"]
    assert res["label"] == "checkout.platform.pagopa.it"
    assert [(p["node_id"], p["rule"]) for p in g.proposals] == [("pagopa-checkout-fe", "frontend")]


def test_conditional_values() -> None:
    sc = Scope({"env": "uat"}, {})
    assert sc.value('${var.env == "prod" ? "platform" : "${var.env}.platform"}') == "uat.platform"
    assert sc.value("${var.missing == 1 ? 2 : 3}") is None


XREF_CORE = {
    "src/net/vnet.tf": """
locals {
  product = "${var.prefix}-${var.env_short}"
  vnets = {
    hub = { peer = [] }
    spoke-tools = { peer = ["hub"] }
  }
}
module "vnet_hub_spoke" {
  source   = "./.terraform/modules/__v4__/virtual_network"
  for_each = local.vnets
  name     = "${local.product}-itn-${each.key}-vnet"
}
resource "azurerm_subnet" "tools" {
  name                 = "tools"
  virtual_network_name = module.vnet_hub_spoke["spoke-tools"].name
}
""",
    "src/net/99_variables.tf": 'variable "prefix" {\n  default = "pagopa"\n}\n',
    "src/net/env/itn-prod/terraform.tfvars": 'env_short = "p"\n',
    "src/apim/product.tf": """
module "apim_gpd_product" {
  source     = "./.terraform/modules/__v3__/api_management_product"
  product_id = "product-gpd"
}
module "pdf_engine" {
  source = "./.terraform/modules/__v4__/app_service"
  name   = "pagopa-p-app-pdf-engine"
}
resource "azurerm_kusto_cluster" "dexp" {
  name = replace(format("%sdataexplorer", "pagopa-p"), "-", "")
}
module "evh_config" {
  source = "./.terraform/modules/__v4__/eventhub_configuration"
  eventhubs = [
    {
      name = "nodo-re"
    }
  ]
}
""",
    "src/apim/env/prod/terraform.tfvars": 'env_short = "p"\n',
}

XREF_APP = {
    "src/domains/x-app/main.tf": """
data "azurerm_api_management_product" "gpd" {
  product_id = "product-gpd"
}
data "azurerm_virtual_network" "tools" {
  name = "pagopa-p-itn-spoke-tools-vnet"
}
data "azurerm_linux_web_app" "pdf" {
  name = "pagopa-p-app-pdf-engine"
}
data "azurerm_kusto_cluster" "dexp" {
  name = lower("PAGOPAPDATAEXPLORER")
}
data "azurerm_eventhub" "re" {
  name = "nodo-re"
}
data "azurerm_nat_gateway" "nat" {
  name = "pagopa-p-natgw"
}
data "azurerm_storage_container" "unknown" {
  name = data.terraform_remote_state.core.outputs.container
}
module "uses" {
  source  = "./.terraform/modules/__v4__/function_app"
  name    = "x"
  a       = data.azurerm_api_management_product.gpd.id
  b       = data.azurerm_virtual_network.tools.id
  c       = data.azurerm_linux_web_app.pdf.id
  d       = data.azurerm_kusto_cluster.dexp.id
  e       = data.azurerm_eventhub.re.id
  f       = data.azurerm_nat_gateway.nat.id
  g       = data.azurerm_storage_container.unknown.id
}
""",
    "src/domains/x-app/env/prod/terraform.tfvars": 'env_short = "p"\n',
}


def test_data_blocks_find_products_loops_functions_and_module_internals() -> None:
    core = parse_repo(XREF_CORE)
    vnets = [
        r["addr"]
        for r in core["stacks"]["src/net"]["resources"]
        if r["tf_type"] == "virtual_network"
    ]
    assert vnets == ['module.vnet_hub_spoke["hub"]', 'module.vnet_hub_spoke["spoke-tools"]']
    g = build({CORE: core, APP: parse_repo(XREF_APP)})
    uses = f"{APP}:src/domains/x-app:module.uses"
    targets = {lk["to_id"] for lk in g.links.values() if lk["from_id"] == uses}
    assert {
        f"{CORE}:src/apim:module.apim_gpd_product",  # matched on product_id
        f'{CORE}:src/net:module.vnet_hub_spoke["spoke-tools"]',  # for_each instance
        f"{CORE}:src/apim:module.pdf_engine",  # app_service module, linux web app data
        f"{CORE}:src/apim:azurerm_kusto_cluster.dexp",  # replace(format()) / lower()
        f"{CORE}:src/apim:module.evh_config",  # event hub created inside the module
    } <= targets
    externals = {r["tf_type"]: r["attrs"]["reason"] for r in g.resources.values() if r["external"]}
    assert externals == {
        "azurerm_nat_gateway": "no_match",
        "azurerm_storage_container": "unresolved_name",
    }
    # a reference to the for_each block reaches its instance as container
    tools = g.resources[f"{CORE}:src/net:azurerm_subnet.tools"]
    assert tools["parent_id"] == f'{CORE}:src/net:module.vnet_hub_spoke["spoke-tools"]'


def test_app_service_running_a_web_frontend_is_found_by_its_image() -> None:
    files = {
        "src/domains/qa-app/web.tf": """
module "qa_hub_app_service" {
  source = "./.terraform/modules/__v4__/IDH/app_service_webapp"
  name   = "pagopa-p-itn-qa-qa-hub-wa"
  app_settings = {
    WEBSITES_PORT       = "3000"
    NEXT_PUBLIC_API_URL = var.api_url
    NEXTAUTH_SECRET     = data.azurerm_key_vault_secret.nextauth.value
  }
  docker_image = var.qa_hub_image.docker_image
}
module "pdf" {
  source       = "./.terraform/modules/__v4__/app_service"
  name         = "pdf"
  docker_image = "ghcr.io/pagopa/pagopapdfengine:1.2.3"
}
""",
        "src/domains/qa-app/env/prod/terraform.tfvars": (
            'qa_hub_image = {\n  docker_image = "pagopa-qa-centralhub-fe"\n}\n'
            'api_url = "https://api.example"\n'
        ),
    }
    ext = parse_repo(files)
    hub = _res(ext, "src/domains/qa-app", "module.qa_hub_app_service")
    assert hub["attrs"] == {"image": "pagopa-qa-centralhub-fe", "web_frontend": True}
    assert "https://api.example" not in str(ext)  # app setting values are never read
    g = build(
        {APP: ext},
        components={"pagopa-qa-centralhub-frontend", "pagopa-pdf-engine"},
        frontends={"pagopa-qa-centralhub-frontend"},
    )
    assert sorted((p["node_id"], p["rule"]) for p in g.proposals) == [
        ("pagopa-pdf-engine", "image"),
        ("pagopa-qa-centralhub-frontend", "image"),
    ]

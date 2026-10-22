"""Read side of the infrastructure view: the payloads of the /ea/infra endpoints."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ea import EaInfraLink, EaInfraResource, EaNode, EaSyncStatus
from app.services.ea.infra.config import ALERT_TYPES, ENVS
from app.services.ea.sync.rules import GITHUB_ORG


def file_url(res: EaInfraResource) -> str | None:
    if not res.file:
        return None
    return f"https://github.com/{GITHUB_ORG}/{res.repo}/blob/HEAD/{res.file}"


def resource_out(res: EaInfraResource, env: str | None = None) -> dict[str, Any]:
    attrs = dict(res.attrs or {})
    location = attrs.pop("location", None) or {}
    names = res.names or {}
    return {
        "id": res.id,
        "label": (names.get(env) if env else None) or res.label,
        "ltype": res.ltype,
        "vendor": res.vendor,
        "tf_type": res.tf_type,
        "address": res.address,
        "repo": res.repo,
        "stack": res.stack,
        "file_url": file_url(res),
        "parent": res.parent_id,
        "blueprint": res.blueprint_id,
        "node": res.ea_node_id,
        "external": res.external,
        "envs": list(res.envs or []),
        "location": location.get(env) if env else None,
        "attrs": attrs,
    }


async def infra_view(db: AsyncSession, env: str) -> dict[str, Any]:
    """Resources, links and monitoring chain of one environment."""
    resources = [
        r
        for r in await db.scalars(
            select(EaInfraResource)
            .where(EaInfraResource.status == "active", EaInfraResource.envs.contains([env]))
            .order_by(EaInfraResource.stack, EaInfraResource.address)
        )
    ]
    ids = {r.id for r in resources}
    links = [
        lk
        for lk in await db.scalars(select(EaInfraLink).where(EaInfraLink.envs.contains([env])))
        if lk.from_id in ids and lk.to_id in ids
    ]
    out_by: dict[str, list[EaInfraLink]] = {}
    for lk in links:
        out_by.setdefault(lk.from_id, []).append(lk)

    alerts, action_groups, dashboards = [], [], []
    for r in resources:
        if r.tf_type in ALERT_TYPES:
            outgoing = out_by.get(r.id, [])
            alerts.append(
                {
                    "id": r.id,
                    "label": (r.names or {}).get(env) or r.label,
                    "severity": (r.attrs or {}).get("severity"),
                    "kind": (r.attrs or {}).get("kind"),
                    "targets": [lk.to_id for lk in outgoing if lk.kind == "monitors"],
                    "action_groups": [lk.to_id for lk in outgoing if lk.kind == "notifies"],
                }
            )
        elif r.ltype == "action_group":
            action_groups.append(
                {
                    "id": r.id,
                    "label": (r.names or {}).get(env) or r.label,
                    "channels": (r.attrs or {}).get("channels") or [],
                }
            )
        elif r.ltype == "dashboard" or r.tf_type == "azurerm_portal_dashboard":
            dashboards.append({"id": r.id, "label": (r.names or {}).get(env) or r.label})

    status = await db.get(EaSyncStatus, 1)
    return {
        "env": env,
        "envs": list(ENVS),
        "synced_at": status.last_success_at if status else None,
        "resources": [resource_out(r, env) for r in resources],
        "links": [
            {
                "from": lk.from_id,
                "to": lk.to_id,
                "kind": lk.kind,
                "origin": lk.origin,
                "evidence": lk.evidence or {},
            }
            for lk in links
        ],
        "monitoring": {
            "alerts": alerts,
            "action_groups": action_groups,
            "dashboards": dashboards,
        },
    }


async def resource_detail(db: AsyncSession, resource_id: str) -> dict[str, Any] | None:
    res = await db.get(EaInfraResource, resource_id)
    if res is None:
        return None
    links = list(
        await db.scalars(
            select(EaInfraLink).where(
                or_(EaInfraLink.from_id == resource_id, EaInfraLink.to_id == resource_id)
            )
        )
    )
    others = {lk.to_id if lk.from_id == resource_id else lk.from_id for lk in links}
    labels = {
        r.id: (r.label, r.ltype)
        for r in await db.scalars(select(EaInfraResource).where(EaInfraResource.id.in_(others)))
    }

    def side(lk: EaInfraLink, other: str) -> dict[str, Any]:
        label, ltype = labels.get(other, (other, "other"))
        return {
            "id": other,
            "label": label,
            "ltype": ltype,
            "kind": lk.kind,
            "origin": lk.origin,
            "envs": list(lk.envs or []),
            "evidence": lk.evidence or {},
        }

    node = await db.get(EaNode, res.ea_node_id) if res.ea_node_id else None
    children = list(
        await db.scalars(
            select(EaInfraResource.id).where(
                EaInfraResource.parent_id == resource_id, EaInfraResource.status == "active"
            )
        )
    )
    return {
        **resource_out(res),
        "names": res.names or {},
        "locations": (res.attrs or {}).get("location") or {},
        "status": res.status,
        "node_name": node.name if node else None,
        "children": len(children),
        "out": [side(lk, lk.to_id) for lk in links if lk.from_id == resource_id],
        "in": [side(lk, lk.from_id) for lk in links if lk.to_id == resource_id],
    }


async def resources_of_node(db: AsyncSession, node_id: str) -> list[dict[str, Any]]:
    """Infrastructure linked to a map node: approved links, or the blueprint category."""
    rows = await db.scalars(
        select(EaInfraResource)
        .where(
            EaInfraResource.status == "active",
            or_(EaInfraResource.ea_node_id == node_id, EaInfraResource.blueprint_id == node_id),
        )
        .order_by(EaInfraResource.label)
        .limit(200)
    )
    return [
        {"id": r.id, "label": r.label, "ltype": r.ltype, "vendor": r.vendor, "envs": r.envs}
        for r in rows
    ]

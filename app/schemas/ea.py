from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class EaKbResponse(BaseModel):
    """Knowledge base in the shape used by the map views (kept loose on purpose)."""

    meta: dict[str, Any]
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    blueprint: dict[str, Any]


class EaImpactItem(BaseModel):
    id: str
    kind: str
    name: str
    depth: int


class EaImpactResponse(BaseModel):
    node_id: str
    depth: int
    upstream: list[EaImpactItem]
    downstream: list[EaImpactItem]


class EaNodeDetail(BaseModel):
    id: str
    kind: str
    parent: str | None
    name: str
    desc: str | None
    attrs: dict[str, Any]
    source: str
    status: str
    verified_at: Any | None
    qa: dict[str, Any] | None
    docs: list[dict[str, Any]]
    out: list[dict[str, Any]]
    in_: list[dict[str, Any]]

    model_config = {"populate_by_name": True}

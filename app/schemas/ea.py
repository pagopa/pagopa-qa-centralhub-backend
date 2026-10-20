from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


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
    verified_by: str | None = None
    qa: dict[str, Any] | None
    docs: list[dict[str, Any]]
    out: list[dict[str, Any]]
    in_: list[dict[str, Any]] = Field(alias="in")
    dq: dict[str, Any] | None = None  # datasets: Data Quality coverage (PR3)

    model_config = {"populate_by_name": True}


class _Actor(BaseModel):
    actor: str | None = None


class EaEdgeCreate(_Actor):
    from_id: str
    to_id: str
    type: str
    label: str | None = None
    note: str


class EaEdgeUpdate(_Actor):
    type: str | None = None
    label: str | None = None
    note: str | None = None


class EaEdgeNote(_Actor):
    note: str | None = None


class EaEdgeOut(BaseModel):
    id: str
    from_: str = Field(alias="from")
    to: str
    type: str
    label: str | None
    origin: str
    status: str
    confidence: int
    note: str | None
    created_by: str | None
    verified_by: str | None
    verified_at: datetime | None

    model_config = {"populate_by_name": True}


class EaNodeUpdate(_Actor):
    name: str | None = None
    description: str | None = None
    parent_id: str | None = None
    ctype: str | None = None
    layer: str | None = None  # datasets only: correct the layer (null = back to the contract)
    doc_url: str | None = None  # datasets only: documentation link (null = back to the contract)
    slack_url: str | None = None  # datasets only: Slack channel link (null = back to the contract)


class EaNodeCreate(_Actor):
    id: str
    kind: str
    name: str
    description: str | None = None
    parent_id: str | None = None
    repo: str | None = None
    ctype: str | None = None


class EaNodeOut(BaseModel):
    id: str
    kind: str
    parent_id: str | None
    name: str
    description: str | None
    source: str
    verified_by: str | None
    verified_at: datetime | None


class EaQaUpdate(_Actor):
    under_qa: bool | None = None
    tas_ci_override: bool | None = None
    qa_owner: str | None = None
    qa_notes: str | None = None


class EaQaOut(BaseModel):
    node_id: str
    under_qa: bool | None
    tas_ci_override: bool | None
    qa_owner: str | None
    qa_notes: str | None
    manual_updated_by: str | None
    manual_updated_at: datetime | None


class EaHistoryItem(BaseModel):
    action: str
    resource_type: str | None
    resource_id: str | None
    actor: str | None
    at: datetime
    detail: dict[str, Any]


# ---- sync and review queue (PR3) -------------------------------------------------------


class EaSyncRequest(_Actor):
    pass


class EaSyncRunOut(BaseModel):
    id: str
    trigger: str
    triggered_by: str | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    stats: dict[str, Any]
    error: str | None


class EaSyncStatusOut(BaseModel):
    running: EaSyncRunOut | None
    last: EaSyncRunOut | None
    last_success_at: datetime | None
    last_error: str | None
    open_changes: int


class EaChangeOut(BaseModel):
    id: str
    run_id: str | None
    entity: str
    op: str
    target_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    structural: bool
    status: str
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_note: str | None
    created_at: datetime


class EaChangeDecision(_Actor):
    note: str | None = None
    # only for a new component: the reviewer can correct the proposal
    parent_id: str | None = None
    ctype: str | None = None
    name: str | None = None


class EaChangeBulk(_Actor):
    ids: list[str] = Field(min_length=1, max_length=200)
    decision: str = Field(pattern="^(approve|reject)$")
    note: str | None = None

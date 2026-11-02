"""Enterprise Architecture map (EA Map) persistence.

The map is a graph of nodes (areas, products, components, blueprint elements, infra)
and typed edges. Every record states where it comes from (``source`` / ``origin``)
and whether a person confirmed it, so that the periodic sync never overwrites a
human decision. See docs/ea-map/DESIGN.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

NODE_KINDS = (
    "area",
    "product",
    "component",
    "external",
    "datastore",
    "segment",
    "service",
    "function",
    "process",
    "infra",
    "dataset",
)
NODE_SOURCES = ("sync", "curated", "manual")
NODE_STATUSES = ("active", "proposed", "archived")

TECH_EDGE_TYPES = ("api", "soap", "event", "data", "ui")
BLUEPRINT_EDGE_TYPES = ("serves", "realizes", "groups", "uses", "runs_on")
# Data Lake (docs/ea-map/DESIGN-data-lake.md): producer → dataset, dataset → dataset,
# dataset → consumer
DATA_EDGE_TYPES = ("produces", "derives", "feeds")
EDGE_TYPES = TECH_EDGE_TYPES + BLUEPRINT_EDGE_TYPES + DATA_EDGE_TYPES
EDGE_ORIGINS = ("helm", "infra_rule", "curated", "manual", "contract", "contract_rule")
EDGE_STATUSES = ("proposed", "active", "verified", "rejected", "removed")
VISIBLE_EDGE_STATUSES = ("active", "verified")

CHANGE_ENTITIES = ("node", "edge", "qa", "doc", "infra")
CHANGE_OPS = ("add", "update", "remove")

# Infrastructure view (docs/ea-map/DESIGN-infra.md)
INFRA_STATUSES = ("active", "removed")
INFRA_LINK_KINDS = ("ref", "monitors", "notifies")
INFRA_LINK_ORIGINS = ("explicit", "inferred")
CHANGE_STATUSES = ("pending", "approved", "rejected", "auto_applied", "superseded", "conflict")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


class EaNode(Base):
    __tablename__ = "ea_nodes"
    __table_args__ = (
        CheckConstraint(_in("kind", NODE_KINDS), name="ck_ea_nodes_kind"),
        CheckConstraint(_in("source", NODE_SOURCES), name="ck_ea_nodes_source"),
        CheckConstraint(_in("status", NODE_STATUSES), name="ck_ea_nodes_status"),
        Index("ix_ea_nodes_kind", "kind"),
        Index("ix_ea_nodes_parent_id", "parent_id"),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        String(200), ForeignKey("ea_nodes.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    attrs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="curated")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EaEdge(Base):
    __tablename__ = "ea_edges"
    __table_args__ = (
        UniqueConstraint("from_id", "to_id", "type", name="uq_ea_edges_from_to_type"),
        CheckConstraint(_in("type", EDGE_TYPES), name="ck_ea_edges_type"),
        CheckConstraint(_in("origin", EDGE_ORIGINS), name="ck_ea_edges_origin"),
        CheckConstraint(_in("status", EDGE_STATUSES), name="ck_ea_edges_status"),
        CheckConstraint("confidence BETWEEN 0 AND 100", name="ck_ea_edges_confidence"),
        Index("ix_ea_edges_from_id", "from_id"),
        Index("ix_ea_edges_to_id", "to_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_id: Mapped[str] = mapped_column(
        String(200), ForeignKey("ea_nodes.id", ondelete="CASCADE"), nullable=False
    )
    to_id: Mapped[str] = mapped_column(
        String(200), ForeignKey("ea_nodes.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    confidence: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=60)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verified_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EaQaSignal(Base):
    """Test signals of a component.

    ``observed`` columns are written only by the sync; ``manual`` columns only by users.
    """

    __tablename__ = "ea_qa_signals"

    node_id: Mapped[str] = mapped_column(
        String(200), ForeignKey("ea_nodes.id", ondelete="CASCADE"), primary_key=True
    )
    # observed
    unit_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    it_features: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    it_workflow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    perf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sonar: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    central_suites: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    nrt_suites: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    nodo_bdd: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tas_ci_observed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # manual
    under_qa: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tas_ci_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    qa_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    qa_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manual_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class EaDocLink(Base):
    __tablename__ = "ea_doc_links"
    __table_args__ = (
        UniqueConstraint("node_id", "url", name="uq_ea_doc_links_node_url"),
        CheckConstraint(_in("source", ("rule", "readme", "manual")), name="ck_ea_doc_links_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    node_id: Mapped[str] = mapped_column(
        String(200), ForeignKey("ea_nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    space: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pages: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="rule")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EaMeta(Base):
    """Key/value store for map-wide content (sources summary, blueprint panels, TAS info)."""

    __tablename__ = "ea_meta"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EaSyncRun(Base):
    __tablename__ = "ea_sync_runs"
    __table_args__ = (
        CheckConstraint(
            _in("trigger", ("beat", "manual", "import")), name="ck_ea_sync_runs_trigger"
        ),
        CheckConstraint(
            _in("status", ("running", "success", "partial", "failed")),
            name="ck_ea_sync_runs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    triggered_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    source_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class EaRepoSnapshot(Base):
    __tablename__ = "ea_repo_snapshots"

    repo: Mapped[str] = mapped_column(String(200), primary_key=True)
    default_branch: Mapped[str] = mapped_column(String(200), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    extracted: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EaChange(Base):
    __tablename__ = "ea_changes"
    __table_args__ = (
        CheckConstraint(_in("entity", CHANGE_ENTITIES), name="ck_ea_changes_entity"),
        CheckConstraint(_in("op", CHANGE_OPS), name="ck_ea_changes_op"),
        CheckConstraint(_in("status", CHANGE_STATUSES), name="ck_ea_changes_status"),
        Index("ix_ea_changes_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ea_sync_runs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    entity: Mapped[str] = mapped_column(String(10), nullable=False)
    op: Mapped[str] = mapped_column(String(10), nullable=False)
    target_id: Mapped[str] = mapped_column(String(300), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    structural: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EaSyncStatus(Base):
    __tablename__ = "ea_sync_status"
    __table_args__ = (CheckConstraint("id = 1", name="ck_ea_sync_status_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ea_sync_runs.id", ondelete="SET NULL"), nullable=True
    )


class EaInfraResource(Base):
    """A Terraform resource of the payments platform (one per stack and address).

    Written by the EA sync from the infra repositories; only ``ea_node_id`` is a human
    decision (an approved link to a component or blueprint node of the map).
    """

    __tablename__ = "ea_infra_resources"
    __table_args__ = (
        CheckConstraint(_in("status", INFRA_STATUSES), name="ck_ea_infra_resources_status"),
        Index("ix_ea_infra_resources_parent_id", "parent_id"),
        Index("ix_ea_infra_resources_ea_node_id", "ea_node_id"),
        Index("ix_ea_infra_resources_blueprint_id", "blueprint_id"),
    )

    id: Mapped[str] = mapped_column(String(400), primary_key=True)
    repo: Mapped[str] = mapped_column(String(200), nullable=False)
    stack: Mapped[str] = mapped_column(String(300), nullable=False)
    address: Mapped[str] = mapped_column(String(300), nullable=False)
    tf_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    tf_type: Mapped[str] = mapped_column(String(120), nullable=False)
    ltype: Mapped[str] = mapped_column(String(40), nullable=False)
    vendor: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    file: Mapped[str | None] = mapped_column(String(500), nullable=True)
    envs: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    names: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    attrs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    parent_id: Mapped[str | None] = mapped_column(String(400), nullable=True)
    blueprint_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ea_node_id: Mapped[str | None] = mapped_column(
        String(200), ForeignKey("ea_nodes.id", ondelete="SET NULL"), nullable=True
    )
    external: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EaInfraLink(Base):
    """An arrow between two infrastructure resources: explicit (a Terraform reference) or
    inferred (e.g. a ``data`` block matched by name in another stack)."""

    __tablename__ = "ea_infra_links"
    __table_args__ = (
        UniqueConstraint("from_id", "to_id", "kind", name="uq_ea_infra_links_from_to_kind"),
        CheckConstraint(_in("kind", INFRA_LINK_KINDS), name="ck_ea_infra_links_kind"),
        CheckConstraint(_in("origin", INFRA_LINK_ORIGINS), name="ck_ea_infra_links_origin"),
        Index("ix_ea_infra_links_to_id", "to_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_id: Mapped[str] = mapped_column(
        String(400), ForeignKey("ea_infra_resources.id", ondelete="CASCADE"), nullable=False
    )
    to_id: Mapped[str] = mapped_column(
        String(400), ForeignKey("ea_infra_resources.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    envs: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class EaEditLog(Base):
    """History of manual changes made from the UI (who, when, before/after)."""

    __tablename__ = "ea_edit_log"
    __table_args__ = (Index("ix_ea_edit_log_target", "entity", "target_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    entity: Mapped[str] = mapped_column(String(10), nullable=False)
    target_id: Mapped[str] = mapped_column(String(300), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

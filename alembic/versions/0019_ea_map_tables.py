"""EA Map persistence tables

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ea_nodes",
        sa.Column("id", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("parent_id", sa.String(length=200), nullable=True),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("attrs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("verified_by", sa.UUID(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('area', 'product', 'component', 'external', 'datastore', "
            "'segment', 'service', 'function', 'process', 'infra')",
            name="ck_ea_nodes_kind",
        ),
        sa.CheckConstraint("source IN ('sync', 'curated', 'manual')", name="ck_ea_nodes_source"),
        sa.CheckConstraint(
            "status IN ('active', 'proposed', 'archived')", name="ck_ea_nodes_status"
        ),
        sa.ForeignKeyConstraint(["parent_id"], ["ea_nodes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["verified_by"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ea_nodes_kind", "ea_nodes", ["kind"], unique=False)
    op.create_index("ix_ea_nodes_parent_id", "ea_nodes", ["parent_id"], unique=False)
    op.create_table(
        "ea_meta",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "ea_repo_snapshots",
        sa.Column("repo", sa.String(length=200), nullable=False),
        sa.Column("default_branch", sa.String(length=200), nullable=False),
        sa.Column("head_sha", sa.String(length=40), nullable=False),
        sa.Column("extracted", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("repo"),
    )
    op.create_table(
        "ea_sync_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("triggered_by", sa.UUID(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'partial', 'failed')", name="ck_ea_sync_runs_status"
        ),
        sa.CheckConstraint(
            "trigger IN ('beat', 'manual', 'import')", name="ck_ea_sync_runs_trigger"
        ),
        sa.ForeignKeyConstraint(
            ["triggered_by"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "ea_edges",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("from_id", sa.String(length=200), nullable=False),
        sa.Column("to_id", sa.String(length=200), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.SmallInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("verified_by", sa.UUID(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "origin IN ('helm', 'infra_rule', 'curated', 'manual')", name="ck_ea_edges_origin"
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'active', 'verified', 'rejected', 'removed')",
            name="ck_ea_edges_status",
        ),
        sa.CheckConstraint(
            "type IN ('api', 'soap', 'event', 'data', 'ui', "
            "'serves', 'realizes', 'groups', 'uses', 'runs_on')",
            name="ck_ea_edges_type",
        ),
        sa.CheckConstraint("confidence BETWEEN 0 AND 100", name="ck_ea_edges_confidence"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["from_id"], ["ea_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_id"], ["ea_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["verified_by"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("from_id", "to_id", "type", name="uq_ea_edges_from_to_type"),
    )
    op.create_index("ix_ea_edges_from_id", "ea_edges", ["from_id"], unique=False)
    op.create_index("ix_ea_edges_to_id", "ea_edges", ["to_id"], unique=False)
    op.create_table(
        "ea_qa_signals",
        sa.Column("node_id", sa.String(length=200), nullable=False),
        sa.Column("unit_files", sa.Integer(), nullable=False),
        sa.Column("it_features", sa.Integer(), nullable=False),
        sa.Column("it_workflow", sa.Boolean(), nullable=False),
        sa.Column("perf", sa.Boolean(), nullable=False),
        sa.Column("e2e", sa.Boolean(), nullable=False),
        sa.Column("sonar", sa.Boolean(), nullable=False),
        sa.Column("central_suites", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("nrt_suites", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("nodo_bdd", sa.Boolean(), nullable=False),
        sa.Column("tas_ci_observed", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("under_qa", sa.Boolean(), nullable=True),
        sa.Column("tas_ci_override", sa.Boolean(), nullable=True),
        sa.Column("qa_owner", sa.String(length=200), nullable=True),
        sa.Column("qa_notes", sa.Text(), nullable=True),
        sa.Column("manual_updated_by", sa.UUID(), nullable=True),
        sa.Column("manual_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["manual_updated_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["node_id"], ["ea_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.create_table(
        "ea_doc_links",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("node_id", sa.String(length=200), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("space", sa.String(length=50), nullable=True),
        sa.Column("pages", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.CheckConstraint("source IN ('rule', 'readme', 'manual')", name="ck_ea_doc_links_source"),
        sa.ForeignKeyConstraint(["node_id"], ["ea_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id", "url", name="uq_ea_doc_links_node_url"),
    )
    op.create_index(op.f("ix_ea_doc_links_node_id"), "ea_doc_links", ["node_id"], unique=False)
    op.create_table(
        "ea_changes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("entity", sa.String(length=10), nullable=False),
        sa.Column("op", sa.String(length=10), nullable=False),
        sa.Column("target_id", sa.String(length=300), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("structural", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("entity IN ('node', 'edge', 'qa', 'doc')", name="ck_ea_changes_entity"),
        sa.CheckConstraint("op IN ('add', 'update', 'remove')", name="ck_ea_changes_op"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'auto_applied', "
            "'superseded', 'conflict')",
            name="ck_ea_changes_status",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["run_id"], ["ea_sync_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ea_changes_run_id"), "ea_changes", ["run_id"], unique=False)
    op.create_index("ix_ea_changes_status", "ea_changes", ["status"], unique=False)
    op.create_table(
        "ea_sync_status",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_run_id", sa.UUID(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_ea_sync_status_singleton"),
        sa.ForeignKeyConstraint(["last_run_id"], ["ea_sync_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("ea_sync_status")
    op.drop_table("ea_changes")
    op.drop_table("ea_doc_links")
    op.drop_table("ea_qa_signals")
    op.drop_table("ea_edges")
    op.drop_table("ea_sync_runs")
    op.drop_table("ea_repo_snapshots")
    op.drop_table("ea_meta")
    op.drop_table("ea_nodes")

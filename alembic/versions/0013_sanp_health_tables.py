"""SANP Health persistence tables

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-16
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    sanp_environment = postgresql.ENUM(
        "SANP", "COLLAUDO", "PRODUZIONE", name="sanp_environment", create_type=False
    )
    sanp_import_status = postgresql.ENUM(
        "complete", "partial", "failed", name="sanp_import_status", create_type=False
    )
    sanp_result_status = postgresql.ENUM(
        "KO",
        "WARNING",
        "INFO",
        "OK",
        "NOT_CONFIGURED",
        name="sanp_result_status",
        create_type=False,
    )
    sanp_severity = postgresql.ENUM(
        "error", "warning", "info", name="sanp_severity", create_type=False
    )
    sanp_environment.create(bind, checkfirst=True)
    sanp_import_status.create(bind, checkfirst=True)
    sanp_result_status.create(bind, checkfirst=True)
    sanp_severity.create(bind, checkfirst=True)

    op.create_table(
        "sanp_health_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("github_run_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("run_number", sa.BigInteger, nullable=False),
        sa.Column("head_sha", sa.String(40), nullable=False),
        sa.Column("workflow_branch", sa.String(255), nullable=False),
        sa.Column("conclusion", sa.String(50), nullable=False),
        sa.Column("html_url", sa.String(2000), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("import_status", sanp_import_status, nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("error_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("warning_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("info_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_sanp_health_runs_completed_at", "sanp_health_runs", ["completed_at"])

    op.create_table(
        "sanp_health_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sanp_health_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("spec_name", sa.String(500), nullable=False),
        sa.Column("environment", sanp_environment, nullable=False),
        sa.Column("source_branch", sa.String(255), nullable=False),
        sa.Column("target_apim", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(500), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("status", sanp_result_status, nullable=False),
        sa.Column("error_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("warning_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("info_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("raw_payload", postgresql.JSONB, nullable=False),
        sa.UniqueConstraint(
            "run_id",
            "spec_name",
            "environment",
            name="uq_sanp_health_results_run_spec_environment",
        ),
    )
    op.create_index("ix_sanp_health_results_run_id", "sanp_health_results", ["run_id"])
    op.create_index(
        "ix_sanp_health_results_environment_status",
        "sanp_health_results",
        ["environment", "status"],
    )

    op.create_table(
        "sanp_health_changes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "result_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sanp_health_results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("level", sa.Integer, nullable=False),
        sa.Column("severity", sanp_severity, nullable=False),
        sa.Column("rule_id", sa.String(255), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=True),
        sa.Column("operation", sa.String(20), nullable=True),
        sa.Column("section", sa.String(255), nullable=True),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("change_order", sa.Integer, nullable=False),
    )
    op.create_index("ix_sanp_health_changes_result_id", "sanp_health_changes", ["result_id"])

    op.create_table(
        "sanp_health_sync_status",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("imported_run_count", sa.Integer, nullable=False, server_default="0"),
        sa.CheckConstraint("id = 1", name="ck_sanp_health_sync_status_singleton"),
    )


def downgrade() -> None:
    op.drop_table("sanp_health_sync_status")
    op.drop_index("ix_sanp_health_changes_result_id", table_name="sanp_health_changes")
    op.drop_table("sanp_health_changes")
    op.drop_index("ix_sanp_health_results_environment_status", table_name="sanp_health_results")
    op.drop_index("ix_sanp_health_results_run_id", table_name="sanp_health_results")
    op.drop_table("sanp_health_results")
    op.drop_index("ix_sanp_health_runs_completed_at", table_name="sanp_health_runs")
    op.drop_table("sanp_health_runs")

    bind = op.get_bind()
    postgresql.ENUM(name="sanp_severity").drop(bind, checkfirst=True)
    postgresql.ENUM(name="sanp_result_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="sanp_import_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="sanp_environment").drop(bind, checkfirst=True)
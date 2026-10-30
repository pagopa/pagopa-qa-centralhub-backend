"""remove E2E feature

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM feature_visibility WHERE key IN ('e2e', 'settings_e2e')")
    op.execute("UPDATE roles SET permissions = permissions - 'view:e2e'")
    op.drop_table("e2e_runs")
    op.drop_table("e2e_suites")
    op.drop_column("ea_qa_signals", "e2e")


def downgrade() -> None:
    op.add_column(
        "ea_qa_signals",
        sa.Column("e2e", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("ea_qa_signals", "e2e", server_default=None)

    op.create_table(
        "e2e_suites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("suite_path", sa.String(200), nullable=False),
        sa.Column("github_repo", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "e2e_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "suite_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("e2e_suites.id"),
            nullable=False,
        ),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("passed", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("allure_url", sa.String(1000), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("suite_id", "run_at", name="uq_e2e_run_suite_run_at"),
    )
    op.create_index("ix_e2e_runs_suite_id_run_at", "e2e_runs", ["suite_id", "run_at"])
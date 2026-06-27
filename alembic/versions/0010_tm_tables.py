"""tm tables - external resources and absences

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_resources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("company", sa.String(200), nullable=False),
        sa.Column("role", sa.String(200), nullable=False),
        sa.Column("daily_rate", sa.Numeric(10, 2), nullable=False),
        sa.Column("contract_start", sa.Date, nullable=False),
        sa.Column("contract_end", sa.Date, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_external_resources_email", "external_resources", ["email"])

    op.create_table(
        "resource_absences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("absence_date", sa.Date, nullable=False),
        sa.Column("absence_type", sa.String(50), nullable=False, server_default="ferie"),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("confluence_event_id", sa.String(255), nullable=True),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["resource_id"], ["external_resources.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_resource_absences_resource_id", "resource_absences", ["resource_id"])
    op.create_index("ix_resource_absences_absence_date", "resource_absences", ["absence_date"])


def downgrade() -> None:
    op.drop_table("resource_absences")
    op.drop_table("external_resources")

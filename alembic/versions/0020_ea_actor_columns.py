"""EA Map: store actors as text (email or name) and add the manual edit log

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("ea_nodes", "verified_by"),
    ("ea_sync_runs", "triggered_by"),
    ("ea_edges", "created_by"),
    ("ea_edges", "verified_by"),
    ("ea_qa_signals", "manual_updated_by"),
    ("ea_changes", "reviewed_by"),
)


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.drop_constraint(f"{table}_{column}_fkey", table, type_="foreignkey")
        op.alter_column(
            table,
            column,
            type_=sa.String(255),
            existing_type=postgresql.UUID(as_uuid=True),
            existing_nullable=True,
            postgresql_using=f"{column}::text",
        )
    op.create_table(
        "ea_edit_log",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("entity", sa.String(length=10), nullable=False),
        sa.Column("target_id", sa.String(length=300), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ea_edit_log_target", "ea_edit_log", ["entity", "target_id"])
    op.create_index(op.f("ix_ea_edit_log_created_at"), "ea_edit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_ea_edit_log_created_at"), table_name="ea_edit_log")
    op.drop_index("ix_ea_edit_log_target", table_name="ea_edit_log")
    op.drop_table("ea_edit_log")
    for table, column in _COLUMNS:
        op.execute(sa.text(f"UPDATE {table} SET {column} = NULL"))  # noqa: S608 (constant identifiers)
        op.alter_column(
            table,
            column,
            type_=postgresql.UUID(as_uuid=True),
            existing_type=sa.String(255),
            existing_nullable=True,
            postgresql_using=f"{column}::uuid",
        )
        op.create_foreign_key(f"{table}_{column}_fkey", table, "users", [column], ["id"])

"""status page change summaries and technology stack caches

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-19
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

_STATUSES = ("OK", "UNAVAILABLE", "ERROR")
_DIRECTIONS = ("forward", "rollback", "diverged", "identical")
_LANGUAGES = ("JAVA", "PYTHON", "NODE", "RUST", "GO")
_SOURCES = ("dockerfile", "build_file")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    op.create_table(
        "sp_change_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("repository", sa.String(200), nullable=False),
        sa.Column("from_version", sa.String(100), nullable=False),
        sa.Column("to_version", sa.String(100), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("direction", sa.String(10), nullable=True),
        sa.Column("from_ref", sa.String(100), nullable=True),
        sa.Column("to_ref", sa.String(100), nullable=True),
        sa.Column(
            "items",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("total_commits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "repository", "from_version", "to_version", name="uq_sp_change_summaries_pair"
        ),
        sa.CheckConstraint(_in("status", _STATUSES), name="ck_sp_change_summaries_status"),
        sa.CheckConstraint(_in("direction", _DIRECTIONS), name="ck_sp_change_summaries_direction"),
    )
    op.create_table(
        "sp_tech_stacks",
        sa.Column("repository", sa.String(200), primary_key=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("language", sa.String(20), nullable=True),
        sa.Column("version", sa.String(20), nullable=True),
        sa.Column("source", sa.String(20), nullable=True),
        sa.Column("for_version", sa.String(100), nullable=False),
        sa.Column("ref", sa.String(100), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(_in("status", _STATUSES), name="ck_sp_tech_stacks_status"),
        sa.CheckConstraint(_in("language", _LANGUAGES), name="ck_sp_tech_stacks_language"),
        sa.CheckConstraint(_in("source", _SOURCES), name="ck_sp_tech_stacks_source"),
    )


def downgrade() -> None:
    op.drop_table("sp_tech_stacks")
    op.drop_table("sp_change_summaries")

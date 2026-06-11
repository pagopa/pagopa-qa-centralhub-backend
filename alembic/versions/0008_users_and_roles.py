"""users and roles tables

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-11
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    roles_table = op.create_table(
        "roles",
        sa.Column("key", sa.String(50), primary_key=True),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("is_system", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("permissions", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.bulk_insert(
        roles_table,
        [
            {"key": "superadmin", "label": "Superadmin", "is_system": True, "permissions": {}},
            {"key": "qa_manager", "label": "QA Manager", "is_system": False, "permissions": {}},
            {"key": "qa_analyst", "label": "QA Analyst", "is_system": False, "permissions": {}},
            {"key": "qa_engineer", "label": "QA Engineer", "is_system": False, "permissions": {}},
            {"key": "guest", "label": "Guest", "is_system": False, "permissions": {}},
        ],
    )

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "role",
            sa.String(50),
            sa.ForeignKey("roles.key"),
            nullable=False,
            server_default="guest",
        ),
        sa.Column("idp_sub", sa.String(255), nullable=True, unique=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_users_email", "users", ["email"])


def downgrade() -> None:
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    op.drop_table("roles")

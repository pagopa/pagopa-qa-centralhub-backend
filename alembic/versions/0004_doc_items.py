"""doc items table

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "doc_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("url", sa.String(2000), nullable=False),
        sa.Column("type", sa.String(20), nullable=False, server_default="external"),
        sa.Column("category", sa.String(100), nullable=False, server_default="Generale"),
        sa.Column("icon", sa.String(50), nullable=False, server_default="page"),
        sa.Column("thumbnail_url", sa.String(2000), nullable=True),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("doc_items")

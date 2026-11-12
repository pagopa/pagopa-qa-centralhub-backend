"""add note to test_runs

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-12
"""
from __future__ import annotations

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS keeps the migration safe on databases patched by hand.
    op.execute("ALTER TABLE qachub.test_runs ADD COLUMN IF NOT EXISTS note VARCHAR")


def downgrade() -> None:
    op.drop_column("test_runs", "note", schema="qachub")

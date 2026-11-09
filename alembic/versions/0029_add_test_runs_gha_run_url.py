"""add gha_run_url to test_runs

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-09
"""
from __future__ import annotations

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS keeps the migration safe on databases patched by hand.
    op.execute("ALTER TABLE qachub.test_runs ADD COLUMN IF NOT EXISTS gha_run_url VARCHAR")


def downgrade() -> None:
    op.drop_column("test_runs", "gha_run_url", schema="qachub")

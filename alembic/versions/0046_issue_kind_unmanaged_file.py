"""test lifecycle: issue kinds for feature files the hub does not manage or whose header is lost

Revision ID: 0046
Revises: 0045
Create Date: 2026-11-27
"""

from __future__ import annotations

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

S = "qachub"


def upgrade() -> None:
    # PostgreSQL >= 12: ADD VALUE is allowed in a transaction (the value is not used here).
    op.execute(f"ALTER TYPE {S}.tl_issue_kind ADD VALUE IF NOT EXISTS 'unmanaged_file'")
    op.execute(f"ALTER TYPE {S}.tl_issue_kind ADD VALUE IF NOT EXISTS 'missing_header'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum without recreating it; the unused values
    # 'unmanaged_file' and 'missing_header' of tl_issue_kind are harmless and stay.
    pass

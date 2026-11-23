"""test lifecycle: feature import source (importa da Git)

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tl_features", sa.Column("import_source", postgresql.JSONB()), schema="qachub"
    )


def downgrade() -> None:
    op.drop_column("tl_features", "import_source", schema="qachub")

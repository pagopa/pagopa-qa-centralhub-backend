"""test lifecycle: working branch of a revision and the shas of its feature file

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, size in (("work_branch", 255), ("work_file_sha", 64), ("work_hub_sha", 64)):
        op.add_column("tl_feature_revisions", sa.Column(name, sa.String(size)), schema="qachub")


def downgrade() -> None:
    op.drop_column("tl_feature_revisions", "work_hub_sha", schema="qachub")
    op.drop_column("tl_feature_revisions", "work_file_sha", schema="qachub")
    op.drop_column("tl_feature_revisions", "work_branch", schema="qachub")

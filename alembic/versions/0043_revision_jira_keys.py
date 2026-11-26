"""test lifecycle: Jira keys per revision (history of design/impl tasks and epics)

Revision ID: 0043
Revises: 0042
Create Date: 2026-11-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

_COLUMNS = ("jira_design_key", "jira_impl_key", "jira_qa_epic_key")

# The first revision of each feature takes the feature's keys; so does the current revision
# (working, else published), which shared them until now and which the feature columns mirror.
BACKFILL_SQL = """
UPDATE qachub.tl_feature_revisions r
SET jira_design_key = f.jira_design_key,
    jira_impl_key = f.jira_impl_key,
    jira_qa_epic_key = f.jira_qa_epic_key
FROM qachub.tl_features f
WHERE r.feature_id = f.id
  AND (
    r.number = (SELECT min(number) FROM qachub.tl_feature_revisions WHERE feature_id = f.id)
    OR r.id = COALESCE(f.working_revision_id, f.published_revision_id)
  )
  AND r.jira_design_key IS NULL AND r.jira_impl_key IS NULL AND r.jira_qa_epic_key IS NULL
"""


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "tl_feature_revisions", sa.Column(name, sa.String(32)), schema="qachub"
        )
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("tl_feature_revisions", name, schema="qachub")

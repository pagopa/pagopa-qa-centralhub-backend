"""test lifecycle jira: scope epic/component, feature jira cache, issue kind

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

S = "qachub"


def upgrade() -> None:
    # PostgreSQL >= 12: ADD VALUE ammesso in transazione (il valore non viene usato qui).
    op.execute(f"ALTER TYPE {S}.tl_issue_kind ADD VALUE IF NOT EXISTS 'jira_creation_failed'")
    op.add_column("tl_scopes", sa.Column("jira_qa_epic_key", sa.String(32)), schema=S)
    op.add_column("tl_scopes", sa.Column("jira_component", sa.String(100)), schema=S)
    op.add_column("tl_features", sa.Column("jira_qa_epic_key", sa.String(32)), schema=S)
    op.add_column(
        "tl_features",
        sa.Column(
            "jira_cache",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        schema=S,
    )
    op.add_column("tl_features", sa.Column("jira_error", sa.Text()), schema=S)


def downgrade() -> None:
    op.drop_column("tl_features", "jira_error", schema=S)
    op.drop_column("tl_features", "jira_cache", schema=S)
    op.drop_column("tl_features", "jira_qa_epic_key", schema=S)
    op.drop_column("tl_scopes", "jira_component", schema=S)
    op.drop_column("tl_scopes", "jira_qa_epic_key", schema=S)
    # Il valore 'jira_creation_failed' di tl_issue_kind resta: PostgreSQL non consente di
    # rimuovere un valore da un enum senza ricrearlo, ed è innocuo se inutilizzato.

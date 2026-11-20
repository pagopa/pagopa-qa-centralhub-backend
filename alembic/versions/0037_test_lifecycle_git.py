"""test lifecycle git: PR/origin columns, issues, sync state, step phrases

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-20
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

S = "qachub"

ENUMS = {
    "tl_issue_kind": (
        "structural_change",
        "new_scenario_from_git",
        "file_deleted",
        "unknown_feature_code",
        "parse_error",
        "pr_closed_unmerged",
        "pr_creation_failed",
    ),
    "tl_issue_status": ("open", "acknowledged", "resolved"),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*ENUMS[name], name=name, schema=S, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name in ENUMS:
        _enum(name).create(bind, checkfirst=True)

    op.add_column("tl_features", sa.Column("git_path", sa.String(500)), schema=S)
    op.add_column("tl_features", sa.Column("git_blob_sha", sa.String(64)), schema=S)

    rev = "tl_feature_revisions"
    op.add_column(
        rev,
        sa.Column("origin", sa.String(10), nullable=False, server_default="hub"),
        schema=S,
    )
    op.add_column(rev, sa.Column("pr_number", sa.Integer), schema=S)
    op.add_column(rev, sa.Column("pr_url", sa.String(1024)), schema=S)
    op.add_column(rev, sa.Column("pr_branch", sa.String(255)), schema=S)
    op.add_column(rev, sa.Column("pr_state", sa.String(10)), schema=S)
    op.add_column(rev, sa.Column("pr_error", sa.Text), schema=S)
    op.add_column(rev, sa.Column("base_commit_sha", sa.String(64)), schema=S)
    op.add_column(rev, sa.Column("merged_commit_sha", sa.String(64)), schema=S)
    op.add_column(
        rev,
        sa.Column("base_stale", sa.Boolean, nullable=False, server_default=sa.false()),
        schema=S,
    )

    op.create_table(
        "tl_issues",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "feature_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.tl_features.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.tl_feature_revisions.id", ondelete="SET NULL"),
        ),
        sa.Column("kind", _enum("tl_issue_kind"), nullable=False),
        sa.Column("status", _enum("tl_issue_status"), nullable=False),
        sa.Column("path", sa.String(500)),
        sa.Column("commit_sha", sa.String(64)),
        sa.Column(
            "detail", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_by", sa.String(255)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema=S,
    )
    op.create_index(
        "ix_tl_issues_status_created_at", "tl_issues", ["status", "created_at"], schema=S
    )

    op.create_table(
        "tl_git_sync_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("last_main_sha", sa.String(64)),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column("last_summary", postgresql.JSONB),
        schema=S,
    )

    op.create_table(
        "tl_step_phrases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("keyword", sa.String(10), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("occurrences", sa.Integer, nullable=False, server_default="0"),
        sa.Column("implemented", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("keyword", "text", name="uq_tl_step_phrases_keyword_text"),
        schema=S,
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_table("tl_step_phrases", schema=S)
    op.drop_table("tl_git_sync_state", schema=S)
    op.drop_table("tl_issues", schema=S)
    rev = "tl_feature_revisions"
    for col in (
        "base_stale",
        "merged_commit_sha",
        "base_commit_sha",
        "pr_error",
        "pr_state",
        "pr_branch",
        "pr_url",
        "pr_number",
        "origin",
    ):
        op.drop_column(rev, col, schema=S)
    op.drop_column("tl_features", "git_blob_sha", schema=S)
    op.drop_column("tl_features", "git_path", schema=S)
    for name in reversed(list(ENUMS)):
        _enum(name).drop(bind, checkfirst=True)

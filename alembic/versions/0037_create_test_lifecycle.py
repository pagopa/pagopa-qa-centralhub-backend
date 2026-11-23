"""test lifecycle: scopes, features, revisions, scenarios, versions, review comments

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
    "tl_feature_status": ("draft", "in_review", "designed", "implemented", "running"),
    "tl_revision_status": ("draft", "in_review", "approved", "merged", "superseded"),
    "tl_scenario_kind": ("scenario", "outline"),
    "tl_polarity": ("positivo", "negativo"),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*ENUMS[name], name=name, schema=S, create_type=False)


def _pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _ts(name: str = "created_at") -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {S}")
    for name in ENUMS:
        _enum(name).create(bind, checkfirst=True)

    op.create_table(
        "tl_scopes",
        _pk(),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("area", sa.String(255)),
        sa.Column("repo_folder", sa.String(255)),
        sa.Column("owner_team", sa.String(255)),
        sa.Column("next_feature_number", sa.Integer, nullable=False, server_default="1"),
        _ts("created_at"),
        _ts("updated_at"),
        schema=S,
    )
    op.create_index(
        "uq_tl_scopes_code_lower", "tl_scopes", [sa.text("lower(code)")], unique=True, schema=S
    )

    op.create_table(
        "tl_features",
        _pk(),
        sa.Column(
            "scope_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.tl_scopes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("status", _enum("tl_feature_status"), nullable=False),
        sa.Column("design_owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("impl_owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("jira_design_key", sa.String(32)),
        sa.Column("jira_impl_key", sa.String(32)),
        sa.Column("working_revision_id", postgresql.UUID(as_uuid=True)),
        sa.Column("published_revision_id", postgresql.UUID(as_uuid=True)),
        sa.Column("next_scenario_number", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(255), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("scope_id", "number", name="uq_tl_features_scope_number"),
        schema=S,
    )
    op.create_index("ix_tl_features_scope_status", "tl_features", ["scope_id", "status"], schema=S)

    op.create_table(
        "tl_feature_revisions",
        _pk(),
        sa.Column(
            "feature_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.tl_features.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("status", _enum("tl_revision_status"), nullable=False),
        sa.Column(
            "base_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.tl_feature_revisions.id"),
        ),
        sa.Column("lock_version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("background_steps", sa.Text),
        sa.Column("requirements_url", sa.String(1024)),
        sa.Column("product_issue_key", sa.String(32)),
        sa.Column("reverse_engineering", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("design_estimate_days", sa.Numeric(5, 1)),
        sa.Column("impl_estimate_days", sa.Numeric(5, 1)),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("approved_by", sa.String(255)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("feature_id", "number", name="uq_tl_revisions_feature_number"),
        schema=S,
    )

    # Circular FK: features -> revisions, added once both tables exist.
    for col, name in (
        ("working_revision_id", "fk_tl_features_working_revision"),
        ("published_revision_id", "fk_tl_features_published_revision"),
    ):
        op.create_foreign_key(
            name,
            "tl_features",
            "tl_feature_revisions",
            [col],
            ["id"],
            source_schema=S,
            referent_schema=S,
        )

    op.create_table(
        "tl_scenarios",
        _pk(),
        sa.Column(
            "feature_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.tl_features.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("tag", sa.String(64), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("feature_id", "number", name="uq_tl_scenarios_feature_number"),
        schema=S,
    )

    op.create_table(
        "tl_scenario_contents",
        _pk(),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.tl_feature_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scenario_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.tl_scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("kind", _enum("tl_scenario_kind"), nullable=False),
        sa.Column("polarity", _enum("tl_polarity"), nullable=False),
        sa.Column(
            "extra_tags",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("steps", sa.Text, nullable=False),
        sa.Column("examples", postgresql.JSONB),
        sa.Column("detail_link", sa.String(1024)),
        sa.UniqueConstraint("revision_id", "scenario_id", name="uq_tl_contents_revision_scenario"),
        schema=S,
    )
    op.create_index(
        "ix_tl_scenario_contents_revision_position",
        "tl_scenario_contents",
        ["revision_id", "position"],
        schema=S,
    )

    op.create_table(
        "tl_feature_versions",
        _pk(),
        sa.Column(
            "feature_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.tl_features.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.tl_feature_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("origin", sa.String(10), nullable=False),
        sa.Column("snapshot", postgresql.JSONB, nullable=False),
        sa.Column("commit_sha", sa.String(64)),
        sa.Column("actor", sa.String(255), nullable=False),
        _ts("created_at"),
        schema=S,
    )
    op.create_index(
        "ix_tl_feature_versions_feature_id", "tl_feature_versions", ["feature_id"], schema=S
    )

    op.create_table(
        "tl_review_comments",
        _pk(),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.tl_feature_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scenario_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.tl_scenarios.id", ondelete="SET NULL"),
        ),
        sa.Column("author", sa.String(255), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("resolved", sa.Boolean, nullable=False, server_default=sa.false()),
        _ts("created_at"),
        schema=S,
    )
    op.create_index(
        "ix_tl_review_comments_revision_id", "tl_review_comments", ["revision_id"], schema=S
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_table("tl_review_comments", schema=S)
    op.drop_table("tl_feature_versions", schema=S)
    op.drop_table("tl_scenario_contents", schema=S)
    op.drop_table("tl_scenarios", schema=S)
    op.drop_constraint(
        "fk_tl_features_working_revision", "tl_features", schema=S, type_="foreignkey"
    )
    op.drop_constraint(
        "fk_tl_features_published_revision", "tl_features", schema=S, type_="foreignkey"
    )
    op.drop_table("tl_feature_revisions", schema=S)
    op.drop_table("tl_features", schema=S)
    op.drop_table("tl_scopes", schema=S)
    for name in reversed(list(ENUMS)):
        _enum(name).drop(bind, checkfirst=True)

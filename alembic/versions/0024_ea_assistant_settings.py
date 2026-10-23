"""Runtime provider settings for the EA Map assistant."""

import sqlalchemy as sa

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ea_assistant_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("gemini_model", sa.String(100), nullable=False),
        sa.Column("groq_model", sa.String(100), nullable=False),
        sa.Column("fallback_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.CheckConstraint("id = 1", name="ck_ea_assistant_settings_singleton"),
        sa.CheckConstraint("provider IN ('gemini', 'groq')", name="ck_ea_assistant_provider"),
    )


def downgrade() -> None:
    op.drop_table("ea_assistant_settings")

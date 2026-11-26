"""test lifecycle: feature test type and exactly one status tag per scenario

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-26
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

# Test type implied by the scope's repository folder (`src/<type>/...`); never overwrites a value.
BACKFILL_TEST_TYPE_SQL = """
UPDATE qachub.tl_features f
SET test_type = m.t
FROM (
    SELECT s.id AS scope_id,
           (regexp_match(trim(both '/' from coalesce(s.repo_folder, '')),
                         '^src/(integration|e2e|api)(/|$)'))[1] AS t
    FROM qachub.tl_scopes s
) m
WHERE f.scope_id = m.scope_id AND m.t IS NOT NULL AND f.test_type IS NULL
"""

# Frozen copy of the rules of services/test_lifecycle/tags.py (migrations must not follow later
# changes of application code): one status tag with precedence skip > broken > runnable, none
# present gives runnable, polarity names are dropped, duplicates removed, order kept.
_STATUS = ("runnable", "skip", "broken")
_PRECEDENCE = ("skip", "broken", "runnable")
_POLARITY = ("positive", "negative", "positivo", "negativo")


def _normalize(tags: list[str]) -> list[str]:
    given = list(dict.fromkeys(t for t in tags if t not in _POLARITY))
    present = [t for t in given if t in _STATUS]
    chosen = next((s for s in _PRECEDENCE if s in present), "runnable")
    out = [t for t in given if t not in _STATUS or t == chosen]
    if chosen not in out:
        out.append(chosen)
    return out


def backfill_status_tags(bind) -> None:
    """Rewrite `extra_tags` of the scenarios of DRAFT revisions that do not already comply.

    Published, merged, superseded, in-review and approved revisions are left as they are: their
    content mirrors the file already in the repository, and rewriting it would make the next Git
    import see a difference and open a new revision.
    """
    rows = bind.execute(
        sa.text(
            "SELECT c.id, c.extra_tags FROM qachub.tl_scenario_contents c "
            "JOIN qachub.tl_feature_revisions r ON r.id = c.revision_id "
            "WHERE r.status = 'draft'"
        )
    ).fetchall()
    for row_id, tags in rows:
        current = list(tags or [])
        fixed = _normalize(current)
        if fixed != current:
            bind.execute(
                sa.text(
                    "UPDATE qachub.tl_scenario_contents "
                    "SET extra_tags = CAST(:tags AS jsonb) WHERE id = :id"
                ),
                {"tags": json.dumps(fixed), "id": row_id},
            )


def upgrade() -> None:
    op.add_column("tl_features", sa.Column("test_type", sa.String(16)), schema="qachub")
    op.create_check_constraint(
        "ck_tl_features_test_type",
        "tl_features",
        "test_type IN ('integration', 'e2e', 'api')",
        schema="qachub",
    )
    op.execute(BACKFILL_TEST_TYPE_SQL)
    backfill_status_tags(op.get_bind())


def downgrade() -> None:
    op.drop_constraint("ck_tl_features_test_type", "tl_features", schema="qachub", type_="check")
    op.drop_column("tl_features", "test_type", schema="qachub")

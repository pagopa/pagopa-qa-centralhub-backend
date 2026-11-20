"""status page: re-detect the Java stacks read from a Dockerfile

The runtime parser read the Java major from the tag of derived images such as
``ubi8/openjdk-17:1.14`` and stored ``JAVA 1``. Dropping the cached Java rows read from a
Dockerfile makes the next enrichment run detect them again with the fixed parser. The table is
a cache, so nothing is lost.

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-20
"""

from __future__ import annotations

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM sp_tech_stacks WHERE language = 'JAVA' AND source = 'dockerfile'")


def downgrade() -> None:
    # Cache rows only: they are detected again by the enrichment task.
    pass

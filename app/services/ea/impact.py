"""Impact analysis on the EA Map graph using a recursive CTE.

Walks visible edges from a node in both directions (what it depends on and what depends
on it) up to ``depth`` hops. Hierarchy (parent/child) is not followed: it is containment,
not dependency.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

_IMPACT_SQL = text(
    """
    WITH RECURSIVE
    down(node_id, depth, path) AS (
        SELECT CAST(:start AS varchar), 0, ARRAY[CAST(:start AS varchar)]
        UNION ALL
        SELECT e.to_id, d.depth + 1, d.path || e.to_id
        FROM down d
        JOIN ea_edges e ON e.from_id = d.node_id AND e.status IN ('active', 'verified')
        WHERE d.depth < :depth AND NOT e.to_id = ANY(d.path)
    ),
    up(node_id, depth, path) AS (
        SELECT CAST(:start AS varchar), 0, ARRAY[CAST(:start AS varchar)]
        UNION ALL
        SELECT e.from_id, u.depth + 1, u.path || e.from_id
        FROM up u
        JOIN ea_edges e ON e.to_id = u.node_id AND e.status IN ('active', 'verified')
        WHERE u.depth < :depth AND NOT e.from_id = ANY(u.path)
    )
    SELECT 'down' AS direction, n.id, n.kind, n.name, min(d.depth) AS depth
    FROM down d JOIN ea_nodes n ON n.id = d.node_id
    WHERE d.depth > 0
    GROUP BY n.id, n.kind, n.name
    UNION ALL
    SELECT 'up' AS direction, n.id, n.kind, n.name, min(u.depth) AS depth
    FROM up u JOIN ea_nodes n ON n.id = u.node_id
    WHERE u.depth > 0
    GROUP BY n.id, n.kind, n.name
    ORDER BY direction, depth, name
    """
)


async def impact(db: AsyncSession, node_id: str, depth: int = 3) -> dict[str, list[dict[str, Any]]]:
    try:
        rows = (await db.execute(_IMPACT_SQL, {"start": node_id, "depth": depth})).mappings().all()
        if rows is None or not rows:
            logger.info("impact query succeeded with 0 results")
        result: dict[str, list[dict[str, Any]]] = {"upstream": [], "downstream": []}
        for row in rows:
            key = "downstream" if row["direction"] == "down" else "upstream"
            result[key].append(
                {"id": row["id"], "kind": row["kind"], "name": row["name"], "depth": row["depth"]}
            )
        return result
    except Exception:
        logger.exception("impact failed")
        raise

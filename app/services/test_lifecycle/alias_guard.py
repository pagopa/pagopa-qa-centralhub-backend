"""Historical scenario tags (aliases) keep their scope code and feature numbers reserved.

An alias such as `A_004_01` still identifies a scenario in the NRT executions: a scope with code
`A`, or a feature numbered 4 in it, would produce the same tag for a different scenario.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import TlFeature, TlScenario, TlScenarioAlias


async def code_held_by_aliases(
    db: AsyncSession, scope_code: str, own_scope_id: uuid.UUID | None = None
) -> bool:
    """True when an alias with this scope code belongs to a feature outside `own_scope_id`."""
    stmt = (
        select(TlScenarioAlias.id)
        .join(TlScenario, TlScenario.id == TlScenarioAlias.scenario_id)
        .join(TlFeature, TlFeature.id == TlScenario.feature_id)
        .where(
            func.upper(TlScenarioAlias.alias).startswith(f"{scope_code.upper()}_", autoescape=True)
        )
    )
    if own_scope_id is not None:
        stmt = stmt.where(TlFeature.scope_id != own_scope_id)
    return (await db.execute(stmt.limit(1))).first() is not None


async def number_held_by_aliases(db: AsyncSession, scope_code: str, number: int) -> bool:
    """True when an alias already uses `<scope code>_<number>_` (a feature number's tags)."""
    prefix = f"{scope_code.upper()}_{number:03d}_"
    stmt = select(TlScenarioAlias.id).where(
        func.upper(TlScenarioAlias.alias).startswith(prefix, autoescape=True)
    )
    return (await db.execute(stmt.limit(1))).first() is not None

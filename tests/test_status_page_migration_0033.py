from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.models.status_page import SpApplication, SpProduct, SpVersionState
from tests._db import TestSession

PREFIX = "T33-"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Any]:
    async with TestSession() as session:
        yield session
    async with TestSession() as s:
        ids = (await s.scalars(select(SpProduct.id).where(SpProduct.name.like(f"{PREFIX}%")))).all()
        await s.execute(delete(SpApplication).where(SpApplication.product_id.in_(ids)))
        await s.execute(delete(SpProduct).where(SpProduct.id.in_(ids)))
        await s.commit()


def _n() -> str:
    return PREFIX + uuid.uuid4().hex[:8]


async def _make(db: Any, **app_kw: Any) -> tuple[SpProduct, SpApplication]:
    product = SpProduct(name=_n(), area="CORE")
    db.add(product)
    await db.flush()
    app = SpApplication(
        product_id=product.id,
        name=_n(),
        product_key=_n(),
        repository="pagopa/x",
        runtime="AKS",
        ci="GHA",
        **app_kw,
    )
    db.add(app)
    await db.commit()
    return product, app


@pytest.mark.asyncio
async def test_defaults(db: Any) -> None:
    product, app = await _make(db)
    async with TestSession() as s:
        p = await s.get(SpProduct, product.id)
        a = await s.get(SpApplication, app.id)
        assert p is not None and a is not None
        assert p.hidden is False
        assert a.hidden is False
        assert a.version_source == "AUTO"


@pytest.mark.asyncio
async def test_manual_read_status_commits(db: Any) -> None:
    _, app = await _make(db, version_source="MANUAL")
    db.add(
        SpVersionState(
            application_id=app.id,
            target="DEV",
            version="1.0.0",
            read_status="MANUAL",
            checked_at=datetime.now(UTC),
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_invalid_version_source_rejected(db: Any) -> None:
    with pytest.raises(IntegrityError):
        await _make(db, version_source="X")
    await db.rollback()

from __future__ import annotations

# ruff: noqa: S101
import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.models.ea import EaNode
from app.models.status_page import (
    SpApplication,
    SpDeployEvent,
    SpProduct,
    SpVersionState,
)
from tests._db import TestSession

_MIGRATION = Path(__file__).parent.parent / "alembic" / "versions" / "0032_status_page_tables.py"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0032", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mig = _load_migration()


def test_seed_matches_status_page_catalog() -> None:
    assert len(mig.SEED_PRODUCTS) == 14
    assert len(mig.SEED_APPLICATIONS) == 53
    assert sum(1 for _, area in mig.SEED_PRODUCTS if area == "TOUCHPOINT") == 1
    names = {p for p, _ in mig.SEED_PRODUCTS} | {a["product"] for a in mig.SEED_APPLICATIONS}
    assert "GPS - Donations" not in names
    keys = [a["product_key"] for a in mig.SEED_APPLICATIONS]
    assert len(keys) == len(set(keys))
    pdf = [a for a in mig.SEED_APPLICATIONS if a["name"] == "PDF Engine"]
    assert {a["product"] for a in pdf} == {"Ricevute PDF", "Stampa Avvisi"}


def _product(suffix: str) -> SpProduct:
    return SpProduct(name=f"test-prod-{suffix}", area="CORE")


def _application(product_id: uuid.UUID, name: str, suffix: str) -> SpApplication:
    return SpApplication(
        product_id=product_id,
        name=name,
        product_key=f"test-key-{suffix}",
        repository="test-repo",
        runtime="AKS",
        ci="GHA",
        kind="backend",
    )


async def _cleanup(prefix: str) -> None:
    async with TestSession() as session:
        await session.execute(
            delete(SpApplication).where(SpApplication.product_key.like(f"{prefix}%"))
        )
        await session.execute(delete(SpProduct).where(SpProduct.name.like("test-prod-%")))
        await session.execute(delete(EaNode).where(EaNode.id.like("test-ea-%")))
        await session.commit()


@pytest.fixture
async def clean() -> object:
    await _cleanup("test-key-")
    yield
    await _cleanup("test-key-")


async def test_same_name_allowed_in_two_products(clean: object) -> None:
    async with TestSession() as session:
        p1, p2 = _product("a"), _product("b")
        session.add_all([p1, p2])
        await session.flush()
        session.add_all(
            [_application(p1.id, "PDF Engine", "1"), _application(p2.id, "PDF Engine", "2")]
        )
        await session.commit()


async def test_same_name_in_same_product_rejected(clean: object) -> None:
    async with TestSession() as session:
        p1 = _product("a")
        session.add(p1)
        await session.flush()
        session.add_all(
            [_application(p1.id, "PDF Engine", "1"), _application(p1.id, "PDF Engine", "2")]
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_product_with_applications_cannot_be_deleted(clean: object) -> None:
    async with TestSession() as session:
        p1 = _product("a")
        session.add(p1)
        await session.flush()
        session.add(_application(p1.id, "App", "1"))
        await session.commit()
        await session.delete(p1)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_application_delete_cascades_state_and_events(clean: object) -> None:
    from datetime import UTC, datetime

    async with TestSession() as session:
        p1 = _product("a")
        session.add(p1)
        await session.flush()
        app = _application(p1.id, "App", "1")
        session.add(app)
        await session.flush()
        now = datetime.now(UTC)
        session.add(
            SpVersionState(
                application_id=app.id,
                target="PROD",
                version="1.0.0",
                read_status="OK",
                checked_at=now,
            )
        )
        session.add(
            SpDeployEvent(
                application_id=app.id,
                target="PROD",
                version="1.0.0",
                detected_at=now,
                is_baseline=True,
            )
        )
        await session.commit()
        app_id = app.id
        await session.delete(app)
        await session.commit()
        states = (
            await session.execute(
                select(SpVersionState).where(SpVersionState.application_id == app_id)
            )
        ).all()
        events = (
            await session.execute(
                select(SpDeployEvent).where(SpDeployEvent.application_id == app_id)
            )
        ).all()
        assert states == [] and events == []


async def test_ea_node_delete_sets_null(clean: object) -> None:
    async with TestSession() as session:
        node = EaNode(id="test-ea-1", name="n", kind="component", source="manual", status="active")
        p1 = _product("a")
        session.add_all([node, p1])
        await session.flush()
        app = _application(p1.id, "App", "1")
        app.ea_node_id = node.id
        session.add(app)
        await session.commit()
        await session.delete(node)
        await session.commit()
        await session.refresh(app)
        assert app.ea_node_id is None

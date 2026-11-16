from __future__ import annotations

# ruff: noqa: S101
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete, select

from app.models.audit_log import AuditLog
from app.models.ea import EaNode
from app.models.status_page import SpApplication, SpProduct
from app.models.test_metrics import TestSuite
from app.schemas.status_page import (
    ApplicationIn,
    ApplicationPatch,
    CheckIn,
    DeleteIn,
    ProductIn,
    ProductPatch,
)
from app.services import status_page_catalog as cat
from app.services.status_page_catalog import CatalogConflictError, CatalogNotFoundError
from app.services.status_page_sources import Reading
from tests._db import TestSession

U = "qa@example.com"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Any]:
    async with TestSession() as session:
        yield session
    # cleanup everything created under the test prefix
    async with TestSession() as s:
        ids = (await s.scalars(select(SpProduct.id).where(SpProduct.name.like("T7-%")))).all()
        await s.execute(delete(SpApplication).where(SpApplication.product_id.in_(ids)))
        await s.execute(delete(SpProduct).where(SpProduct.id.in_(ids)))
        await s.execute(
            delete(AuditLog).where(
                AuditLog.action.like("status_page.%"), AuditLog.detail.like("%T7-%")
            )
        )
        await s.execute(delete(TestSuite).where(TestSuite.test_object.like("T7-%")))
        await s.execute(delete(EaNode).where(EaNode.id.like("t7-%")))
        await s.commit()


def _n() -> str:
    return "T7-" + uuid.uuid4().hex[:8]


def _app(product_id: uuid.UUID, **kw: Any) -> ApplicationIn:
    base: dict[str, Any] = {
        "product_id": product_id,
        "name": _n(),
        "product_key": _n(),
        "repository": "repo-x",
        "runtime": "AKS",
        "ci": "GHA",
        "user": U,
    }
    base.update(kw)
    return ApplicationIn(**base)


async def _audits(db: Any, resource_id: str) -> list[AuditLog]:
    return list(
        (await db.scalars(select(AuditLog).where(AuditLog.resource_id == resource_id))).all()
    )


def test_schema_validation() -> None:
    pid = uuid.uuid4()
    with pytest.raises(ValidationError):
        _app(pid, repository="bad repo/x")
    with pytest.raises(ValidationError):
        _app(pid, pipeline_url="http://x")
    with pytest.raises(ValidationError):
        ProductIn(name="  ", area="CORE", user=U)
    assert ProductIn(name=" a ", area="CORE", user=U).name == "a"
    assert _app(pid, pipeline_url="  ", test_object="").pipeline_url is None


async def test_product_crud_and_audit(db: Any) -> None:
    name = _n()
    out = await cat.create_product(db, ProductIn(name=name, area="CORE", user=U))
    assert out.application_count == 0
    with pytest.raises(CatalogConflictError) as ei:
        await cat.create_product(db, ProductIn(name=name, area="CORE", user=U))
    assert ei.value.field == "name"
    assert ei.value.message == "Esiste già un prodotto con questo nome"

    new = _n()
    upd = await cat.update_product(db, out.id, ProductPatch(name=new, user=U))
    assert upd.name == new
    assert any(p.id == out.id for p in await cat.list_products(db))

    audits = await _audits(db, str(out.id))
    actions = sorted(a.action for a in audits)
    assert actions == ["status_page.product.create", "status_page.product.update"]
    upd_audit = next(a for a in audits if a.action.endswith("update"))
    d = json.loads(upd_audit.detail or "")
    assert d == {"user": U, "before": {"name": name}, "after": {"name": new}}
    assert upd_audit.resource_type == "sp_product"

    await cat.delete_product(db, out.id, DeleteIn(user=U))
    with pytest.raises(CatalogNotFoundError):
        await cat.delete_product(db, out.id, DeleteIn(user=U))
    with pytest.raises(CatalogNotFoundError):
        await cat.update_product(db, uuid.uuid4(), ProductPatch(name="x", user=U))
    assert "status_page.product.delete" in [a.action for a in await _audits(db, str(out.id))]


async def test_delete_product_with_applications_refused(db: Any) -> None:
    p = await cat.create_product(db, ProductIn(name=_n(), area="CORE", user=U))
    a = await cat.create_application(db, _app(p.id))
    with pytest.raises(CatalogConflictError) as ei:
        await cat.delete_product(db, p.id, DeleteIn(user=U))
    assert ei.value.field == "product"
    assert ei.value.message == "Il prodotto contiene ancora applicazioni"
    products = {x.id: x for x in await cat.list_products(db)}
    assert products[p.id].application_count == 1
    await cat.delete_application(db, a.id, DeleteIn(user=U))
    await cat.delete_product(db, p.id, DeleteIn(user=U))


async def test_application_conflicts(db: Any) -> None:
    p1 = await cat.create_product(db, ProductIn(name=_n(), area="CORE", user=U))
    p2 = await cat.create_product(db, ProductIn(name=_n(), area="TOUCHPOINT", user=U))
    a = await cat.create_application(db, _app(p1.id, test_object="T7-shared"))

    with pytest.raises(CatalogConflictError) as ei:
        await cat.create_application(db, _app(p2.id, product_key=a.product_key))
    assert ei.value.field == "product_key"
    assert ei.value.message == "Chiave product già usata da un'altra applicazione"

    with pytest.raises(CatalogConflictError) as ei:
        await cat.create_application(db, _app(p1.id, name=a.name))
    assert ei.value.field == "name"
    assert ei.value.message == "Esiste già un'applicazione con questo nome nel prodotto"

    # same name in another product and a shared test_object are fine
    b = await cat.create_application(db, _app(p2.id, name=a.name, test_object="T7-shared"))
    assert b.id != a.id

    # session still usable after the rollbacks
    assert len(await cat.list_applications(db, p1.id)) == 1
    assert len(await cat.list_applications(db, None)) >= 2

    # conflicts on update too
    c = await cat.create_application(db, _app(p1.id))
    with pytest.raises(CatalogConflictError) as ei:
        await cat.update_application(db, c.id, ApplicationPatch(product_key=a.product_key, user=U))
    assert ei.value.field == "product_key"
    with pytest.raises(CatalogConflictError) as ei:
        await cat.update_application(db, c.id, ApplicationPatch(name=a.name, user=U))
    assert ei.value.field == "name"


async def test_application_unknown_product_and_ids(db: Any) -> None:
    with pytest.raises(CatalogNotFoundError):
        await cat.create_application(db, _app(uuid.uuid4()))
    with pytest.raises(CatalogNotFoundError):
        await cat.update_application(db, uuid.uuid4(), ApplicationPatch(name="x", user=U))
    with pytest.raises(CatalogNotFoundError):
        await cat.delete_application(db, uuid.uuid4(), DeleteIn(user=U))
    p = await cat.create_product(db, ProductIn(name=_n(), area="CORE", user=U))
    a = await cat.create_application(db, _app(p.id))
    with pytest.raises(CatalogNotFoundError):
        await cat.update_application(db, a.id, ApplicationPatch(product_id=uuid.uuid4(), user=U))


async def test_update_application_moves_and_audits_changed_only(db: Any) -> None:
    p1 = await cat.create_product(db, ProductIn(name=_n(), area="CORE", user=U))
    p2 = await cat.create_product(db, ProductIn(name=_n(), area="CORE", user=U))
    a = await cat.create_application(db, _app(p1.id, pipeline_url="https://x/y"))
    out = await cat.update_application(
        db, a.id, ApplicationPatch(product_id=p2.id, pipeline_url=None, user=U)
    )
    assert out.product_id == p2.id
    assert out.pipeline_url is None
    audit = next(
        x for x in await _audits(db, str(a.id)) if x.action == "status_page.application.update"
    )
    d = json.loads(audit.detail or "")
    assert d["user"] == U
    assert d["before"] == {"product_id": str(p1.id), "pipeline_url": "https://x/y"}
    assert d["after"] == {"product_id": str(p2.id), "pipeline_url": None}
    created = next(
        x for x in await _audits(db, str(a.id)) if x.action == "status_page.application.create"
    )
    assert created.resource_type == "sp_application"
    await cat.delete_application(db, a.id, DeleteIn(user=U))
    assert "status_page.application.delete" in [x.action for x in await _audits(db, str(a.id))]


async def test_check_application_maps_readings() -> None:
    async def fake_env(_c: Any, target: str, _k: str) -> Reading:
        if target == "DEV":
            return Reading(target, "OK", "1.0.0", None)
        return Reading(target, "ERROR", None, "HTTP 500")

    async def fake_repo(_c: Any, _r: str) -> Reading:
        return Reading("REPO", "NO_RELEASE", None, "no release")

    with (
        patch.object(cat, "read_env", fake_env),
        patch.object(cat, "read_repo", fake_repo),
    ):
        out = await cat.check_application(CheckIn(product_key="k", repository="r"))
    assert out.repo.ok is False and out.repo.detail == "no release"
    assert out.envs["DEV"].ok and out.envs["DEV"].version == "1.0.0"
    assert out.envs["UAT"].ok is False and out.envs["UAT"].detail == "HTTP 500"
    assert set(out.envs) == {"DEV", "UAT", "PROD"}


async def test_list_test_objects_distinct_sorted(db: Any) -> None:
    for obj, ver in (("T7-b", "1"), ("T7-a", "1"), ("T7-b", "2")):
        db.add(TestSuite(test_object=obj, test_type="NRT", test_object_version=ver))
    await db.commit()
    objs = [o for o in await cat.list_test_objects(db) if o.startswith("T7-")]
    assert objs == ["T7-a", "T7-b"]
    full = await cat.list_test_objects(db)
    assert full == sorted(set(full))


async def test_list_ea_components_filters(db: Any) -> None:
    db.add_all(
        [
            EaNode(id="t7-biz-a", kind="component", name="Alpha", status="active"),
            EaNode(id="t7-x", kind="component", name="Big BIZ thing", status="active"),
            EaNode(id="t7-biz-old", kind="component", name="Old", status="archived"),
            EaNode(id="t7-biz-dom", kind="service", name="Dom", status="active"),
        ]
    )
    await db.commit()
    out = await cat.list_ea_components(db, "BIZ")
    ids = {c.id for c in out if c.id.startswith("t7-")}
    assert ids == {"t7-biz-a", "t7-x"}
    assert len(await cat.list_ea_components(db, None)) <= 50


async def test_unknown_ea_node_is_not_found(db: Any) -> None:
    p = await cat.create_product(db, ProductIn(name=_n(), area="CORE", user=U))
    with pytest.raises(CatalogNotFoundError) as ei:
        await cat.create_application(db, _app(p.id, ea_node_id="t7-missing"))
    assert str(ei.value) == "Componente EA non trovato"
    a = await cat.create_application(db, _app(p.id))
    with pytest.raises(CatalogNotFoundError):
        await cat.update_application(db, a.id, ApplicationPatch(ea_node_id="t7-missing", user=U))
    db.add(EaNode(id="t7-real", kind="component", name="Real", status="active"))
    await db.commit()
    created = await cat.create_application(db, _app(p.id, ea_node_id="t7-real"))
    assert created.ea_node_id == "t7-real"
    cleared = await cat.update_application(
        db, created.id, ApplicationPatch(ea_node_id=None, user=U)
    )
    assert cleared.ea_node_id is None

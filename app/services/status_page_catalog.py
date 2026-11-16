"""Catalog management for the Release Status feature: products, applications, probe and audit."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import structlog
from sqlalchemy import distinct, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.ea import EaNode
from app.models.status_page import SpApplication, SpProduct
from app.models.test_metrics import TestSuite
from app.schemas.status_page import (
    ApplicationIn,
    ApplicationOut,
    ApplicationPatch,
    CheckIn,
    CheckOut,
    CheckTargetOut,
    DeleteIn,
    EaComponentOut,
    ProductIn,
    ProductOut,
    ProductPatch,
)
from app.services.status_page_sources import Reading, read_env, read_repo

logger = structlog.get_logger(__name__)

ENV_TARGETS = ("DEV", "UAT", "PROD")
EA_COMPONENT_LIMIT = 50

MSG_PRODUCT_NAME = "Esiste già un prodotto con questo nome"
MSG_PRODUCT_KEY = "Chiave product già usata da un'altra applicazione"
MSG_APP_NAME = "Esiste già un'applicazione con questo nome nel prodotto"
MSG_PRODUCT_NOT_EMPTY = "Il prodotto contiene ancora applicazioni"


class CatalogConflictError(Exception):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class CatalogNotFoundError(Exception):
    pass


def _product_snapshot(p: SpProduct) -> dict[str, Any]:
    return {"name": p.name, "area": p.area}


_APP_FIELDS = (
    "product_id",
    "name",
    "product_key",
    "repository",
    "pipeline_url",
    "runtime",
    "ci",
    "kind",
    "test_object",
    "ea_node_id",
)


def _app_snapshot(a: SpApplication) -> dict[str, Any]:
    return {f: _jsonable(getattr(a, f)) for f in _APP_FIELDS}


def _jsonable(value: Any) -> Any:
    return str(value) if isinstance(value, uuid.UUID) else value


def _audit(
    db: AsyncSession,
    *,
    entity: str,
    verb: str,
    resource_id: uuid.UUID,
    user: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    db.add(
        AuditLog(
            actor_id=None,
            action=f"status_page.{entity}.{verb}",
            resource_type=f"sp_{entity}",
            resource_id=str(resource_id),
            detail=json.dumps({"user": user, "before": before, "after": after}),
        )
    )


def _conflict_from(exc: IntegrityError) -> CatalogConflictError | None:
    text = str(exc.orig)
    if "uq_sp_applications_product_name" in text:
        return CatalogConflictError("name", MSG_APP_NAME)
    if "sp_applications_product_key" in text:
        return CatalogConflictError("product_key", MSG_PRODUCT_KEY)
    if "sp_products_name" in text:
        return CatalogConflictError("name", MSG_PRODUCT_NAME)
    return None


async def _commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        conflict = _conflict_from(exc)
        if conflict is not None:
            raise conflict from exc
        logger.exception("status_page catalog write failed")
        raise
    except Exception:
        await db.rollback()
        logger.exception("status_page catalog write failed")
        raise


async def _get_product(db: AsyncSession, product_id: uuid.UUID) -> SpProduct:
    product = await db.get(SpProduct, product_id)
    if product is None:
        raise CatalogNotFoundError(f"Product {product_id} not found")
    return product


async def _get_application(db: AsyncSession, app_id: uuid.UUID) -> SpApplication:
    app = await db.get(SpApplication, app_id)
    if app is None:
        raise CatalogNotFoundError(f"Application {app_id} not found")
    return app


async def _application_count(db: AsyncSession, product_id: uuid.UUID) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(SpApplication)
            .where(SpApplication.product_id == product_id)
        )
        or 0
    )


# --- products -----------------------------------------------------------------------------------


async def list_products(db: AsyncSession) -> list[ProductOut]:
    try:
        stmt = (
            select(SpProduct, func.count(SpApplication.id))
            .outerjoin(SpApplication, SpApplication.product_id == SpProduct.id)
            .group_by(SpProduct.id)
            .order_by(SpProduct.area, SpProduct.name)
        )
        rows = (await db.execute(stmt)).all()
        if not rows:
            logger.info("status_page catalog query succeeded with 0 results", entity="product")
        return [
            ProductOut(id=p.id, name=p.name, area=p.area, application_count=int(n)) for p, n in rows
        ]
    except Exception:
        logger.exception("status_page list_products failed")
        raise


async def create_product(db: AsyncSession, payload: ProductIn) -> ProductOut:
    product = SpProduct(name=payload.name, area=payload.area)
    db.add(product)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        conflict = _conflict_from(exc)
        if conflict is not None:
            raise conflict from exc
        logger.exception("status_page create_product failed")
        raise
    _audit(
        db,
        entity="product",
        verb="create",
        resource_id=product.id,
        user=payload.user,
        before={},
        after=_product_snapshot(product),
    )
    await _commit(db)
    return ProductOut(id=product.id, name=product.name, area=product.area, application_count=0)


async def update_product(
    db: AsyncSession, product_id: uuid.UUID, payload: ProductPatch
) -> ProductOut:
    product = await _get_product(db, product_id)
    changes = payload.model_dump(exclude_unset=True, exclude={"user"})
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for field, value in changes.items():
        if value is None or getattr(product, field) == value:
            continue
        before[field] = getattr(product, field)
        after[field] = value
        setattr(product, field, value)
    if after:
        _audit(
            db,
            entity="product",
            verb="update",
            resource_id=product.id,
            user=payload.user,
            before=before,
            after=after,
        )
    await _commit(db)
    return ProductOut(
        id=product.id,
        name=product.name,
        area=product.area,
        application_count=await _application_count(db, product.id),
    )


async def delete_product(db: AsyncSession, product_id: uuid.UUID, payload: DeleteIn) -> None:
    product = await _get_product(db, product_id)
    if await _application_count(db, product_id) > 0:
        raise CatalogConflictError("product", MSG_PRODUCT_NOT_EMPTY)
    snapshot = _product_snapshot(product)
    await db.delete(product)
    _audit(
        db,
        entity="product",
        verb="delete",
        resource_id=product_id,
        user=payload.user,
        before=snapshot,
        after={},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        # An application was added between the pre-check and the delete (FK RESTRICT).
        await db.rollback()
        raise CatalogConflictError("product", MSG_PRODUCT_NOT_EMPTY) from exc
    except Exception:
        await db.rollback()
        logger.exception("status_page delete_product failed")
        raise


# --- applications -------------------------------------------------------------------------------


async def list_applications(db: AsyncSession, product_id: uuid.UUID | None) -> list[ApplicationOut]:
    try:
        stmt = select(SpApplication).order_by(SpApplication.name)
        if product_id is not None:
            stmt = stmt.where(SpApplication.product_id == product_id)
        apps = (await db.scalars(stmt)).all()
        if not apps:
            logger.info("status_page catalog query succeeded with 0 results", entity="application")
        return [ApplicationOut.model_validate(a) for a in apps]
    except Exception:
        logger.exception("status_page list_applications failed")
        raise


async def _ensure_ea_node(db: AsyncSession, ea_node_id: str | None) -> None:
    if ea_node_id is not None and await db.get(EaNode, ea_node_id) is None:
        raise CatalogNotFoundError("Componente EA non trovato")


async def create_application(db: AsyncSession, payload: ApplicationIn) -> ApplicationOut:
    await _get_product(db, payload.product_id)
    await _ensure_ea_node(db, payload.ea_node_id)
    data = payload.model_dump(exclude={"user"})
    app = SpApplication(**data)
    db.add(app)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        conflict = _conflict_from(exc)
        if conflict is not None:
            raise conflict from exc
        logger.exception("status_page create_application failed")
        raise
    _audit(
        db,
        entity="application",
        verb="create",
        resource_id=app.id,
        user=payload.user,
        before={},
        after=_app_snapshot(app),
    )
    await _commit(db)
    return ApplicationOut.model_validate(app)


async def update_application(
    db: AsyncSession, app_id: uuid.UUID, payload: ApplicationPatch
) -> ApplicationOut:
    app = await _get_application(db, app_id)
    changes = payload.model_dump(exclude_unset=True, exclude={"user"})
    nullable = {"pipeline_url", "test_object", "ea_node_id"}
    if changes.get("product_id") not in (None, app.product_id):
        await _get_product(db, changes["product_id"])
    await _ensure_ea_node(db, changes.get("ea_node_id"))
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for field, value in changes.items():
        if value is None and field not in nullable:
            continue
        current = getattr(app, field)
        if current == value:
            continue
        before[field] = _jsonable(current)
        after[field] = _jsonable(value)
        setattr(app, field, value)
    if after:
        _audit(
            db,
            entity="application",
            verb="update",
            resource_id=app.id,
            user=payload.user,
            before=before,
            after=after,
        )
    await _commit(db)
    return ApplicationOut.model_validate(app)


async def delete_application(db: AsyncSession, app_id: uuid.UUID, payload: DeleteIn) -> None:
    app = await _get_application(db, app_id)
    snapshot = _app_snapshot(app)
    await db.delete(app)
    _audit(
        db,
        entity="application",
        verb="delete",
        resource_id=app_id,
        user=payload.user,
        before=snapshot,
        after={},
    )
    await _commit(db)


# --- probe and lookups --------------------------------------------------------------------------


def _target_out(reading: Reading) -> CheckTargetOut:
    return CheckTargetOut(
        ok=reading.read_status == "OK", version=reading.version, detail=reading.error_detail
    )


async def check_application(payload: CheckIn) -> CheckOut:
    """Live probe of a product_key/repository pair; never persists anything."""
    try:
        async with httpx.AsyncClient(timeout=settings.status_page_http_timeout_s) as client:
            repo, *envs = await asyncio.gather(
                read_repo(client, payload.repository),
                *(read_env(client, t, payload.product_key) for t in ENV_TARGETS),
            )
        return CheckOut(
            repo=_target_out(repo),
            envs={t: _target_out(r) for t, r in zip(ENV_TARGETS, envs, strict=True)},
        )
    except Exception:
        logger.exception("status_page check_application failed")
        raise


async def list_test_objects(db: AsyncSession) -> list[str]:
    try:
        rows = (
            await db.scalars(
                select(distinct(TestSuite.test_object)).order_by(TestSuite.test_object)
            )
        ).all()
        if not rows:
            logger.info("status_page test objects query succeeded with 0 results")
        return list(rows)
    except Exception:
        logger.exception("status_page list_test_objects failed")
        raise


async def list_ea_components(db: AsyncSession, q: str | None) -> list[EaComponentOut]:
    try:
        stmt = select(EaNode.id, EaNode.name).where(
            EaNode.kind == "component", EaNode.status == "active"
        )
        if q and q.strip():
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(EaNode.id.ilike(like), EaNode.name.ilike(like)))
        stmt = stmt.order_by(EaNode.name, EaNode.id).limit(EA_COMPONENT_LIMIT)
        rows = (await db.execute(stmt)).all()
        if not rows:
            logger.info("status_page ea components query succeeded with 0 results")
        return [EaComponentOut(id=i, name=n) for i, n in rows]
    except Exception:
        logger.exception("status_page list_ea_components failed")
        raise

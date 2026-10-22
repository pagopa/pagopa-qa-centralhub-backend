from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.docs import DocItem

logger = structlog.get_logger(__name__)


async def list_items(db: AsyncSession) -> list[DocItem]:
    try:
        result = await db.execute(select(DocItem).order_by(DocItem.position, DocItem.created_at))
        items = list(result.scalars().all())
        if not items:
            logger.info("list_items query succeeded with 0 results")
        return items
    except Exception:
        logger.exception("list_items failed")
        raise


async def get_item(db: AsyncSession, item_id: uuid.UUID) -> DocItem | None:
    try:
        item = await db.get(DocItem, item_id)
        if item is None:
            logger.info("get_item query succeeded with 0 results")
        return item
    except Exception:
        logger.exception("get_item failed")
        raise


async def create_item(db: AsyncSession, data: dict) -> DocItem:
    try:
        item = DocItem(**data)
        db.add(item)
        await db.commit()
        await db.refresh(item)
        return item
    except Exception:
        logger.exception("create_item failed")
        raise


async def update_item(db: AsyncSession, item: DocItem, data: dict) -> DocItem:
    try:
        if not data:
            logger.info("update_item: no input fields to update")
        for key, value in data.items():
            setattr(item, key, value)
        await db.commit()
        await db.refresh(item)
        return item
    except Exception:
        logger.exception("update_item failed")
        raise


async def delete_item(db: AsyncSession, item: DocItem) -> None:
    try:
        await db.delete(item)
        await db.commit()
    except Exception:
        logger.exception("delete_item failed")
        raise

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.docs import DocItem


async def list_items(db: AsyncSession) -> list[DocItem]:
    result = await db.execute(select(DocItem).order_by(DocItem.position, DocItem.created_at))
    return list(result.scalars().all())


async def get_item(db: AsyncSession, item_id: uuid.UUID) -> DocItem | None:
    return await db.get(DocItem, item_id)


async def create_item(db: AsyncSession, data: dict) -> DocItem:
    item = DocItem(**data)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def update_item(db: AsyncSession, item: DocItem, data: dict) -> DocItem:
    for key, value in data.items():
        setattr(item, key, value)
    await db.commit()
    await db.refresh(item)
    return item


async def delete_item(db: AsyncSession, item: DocItem) -> None:
    await db.delete(item)
    await db.commit()

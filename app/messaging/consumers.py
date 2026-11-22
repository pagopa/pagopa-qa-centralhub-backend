"""Consumer registry: a consumer is a name plus an idempotent async handler.

A handler receives everything it needs in its arguments and never reads the ``msg_*`` delivery
tables. ``session`` is the handler's own database session (used, for example, by the inbox to write
its store); the dispatcher commits it after the handler returns and rolls it back on error.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.messaging.envelope import CloudEvent


@dataclass(frozen=True)
class DeliveryContext:
    params: dict[str, Any]
    delivery_id: uuid.UUID
    attempt: int
    session: AsyncSession


Handler = Callable[[CloudEvent, DeliveryContext], Awaitable[None]]
MergeHook = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]


@dataclass(frozen=True)
class Consumer:
    name: str
    handler: Handler
    # Combines the params of two deliveries of this consumer for the same event into one, or
    # returns None when they must stay separate. Consumers without a hook never merge.
    merge: MergeHook | None = None


CONSUMERS: dict[str, Consumer] = {}


def register_consumer(consumer: Consumer) -> None:
    CONSUMERS[consumer.name] = consumer

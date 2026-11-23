"""QA Messaging Hub: CloudEvents, routing rules, deduplication and consumers.

Producers use ``publish()`` / ``clear_condition()``; only this package reads or writes the
``msg_*`` tables.
"""

from __future__ import annotations

from app.messaging.bus import (
    PublishResult,
    PublishStatus,
    clear_condition,
    publish,
    wake_dispatcher,
)
from app.messaging.catalog import validate_catalog
from app.messaging.consumers import register_consumer
from app.messaging.envelope import CloudEvent, new_event
from app.messaging.inbox import INBOX_CONSUMER
from app.messaging.rules import validate_registry

register_consumer(INBOX_CONSUMER)
# Misconfigured routing (unknown consumer, role or event type) must fail at startup.
validate_catalog()
validate_registry()

__all__ = [
    "CloudEvent",
    "PublishResult",
    "PublishStatus",
    "clear_condition",
    "new_event",
    "publish",
    "wake_dispatcher",
]

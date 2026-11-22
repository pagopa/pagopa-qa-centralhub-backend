from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.messaging.consumers import CONSUMERS


@pytest.fixture
def restore_consumers() -> Iterator[None]:
    """Let a test register throw-away consumers; the registry is restored afterwards."""
    saved = dict(CONSUMERS)
    yield
    CONSUMERS.clear()
    CONSUMERS.update(saved)

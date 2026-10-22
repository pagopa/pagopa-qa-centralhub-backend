from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic
from traceback import extract_tb
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def error_fields(error: Exception) -> dict[str, Any]:
    try:
        return {
            "error_type": type(error).__name__,
            "traceback": [
                {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                for frame in extract_tb(error.__traceback__)
            ],
        }
    except Exception:
        logger.exception("error_fields failed")
        raise


@contextmanager
def trace_phase(log: Any, phase: str, **fields: Any) -> Iterator[None]:
    try:
        started = monotonic()
        log.info("ea_sync_phase_started", phase=phase, **fields)
        try:
            yield
        except Exception as exc:
            logger.exception("trace_phase failed")
            log.error(
                "ea_sync_phase_failed",
                phase=phase,
                duration_ms=round((monotonic() - started) * 1000),
                **fields,
                **error_fields(exc),
            )
            raise
        else:
            log.info(
                "ea_sync_phase_completed",
                phase=phase,
                duration_ms=round((monotonic() - started) * 1000),
                **fields,
            )
    except Exception:
        logger.exception("trace_phase failed")
        raise

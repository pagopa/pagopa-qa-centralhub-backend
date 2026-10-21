from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic
from traceback import extract_tb
from typing import Any


def error_fields(error: Exception) -> dict[str, Any]:
    return {
        "error_type": type(error).__name__,
        "traceback": [
            {"file": frame.filename, "line": frame.lineno, "function": frame.name}
            for frame in extract_tb(error.__traceback__)
        ],
    }


@contextmanager
def trace_phase(log: Any, phase: str, **fields: Any) -> Iterator[None]:
    started = monotonic()
    log.info("ea_sync_phase_started", phase=phase, **fields)
    try:
        yield
    except Exception as exc:
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

"""Valori nei log senza a capo: una voce di log non può fingerne un'altra (log injection)."""

from __future__ import annotations

from typing import Any


def log_safe(value: Any) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")

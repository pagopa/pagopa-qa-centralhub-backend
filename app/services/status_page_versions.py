"""SemVer 2.0 comparison used by the Release Status feature (pure, no I/O)."""

from __future__ import annotations

import re
from enum import StrEnum

_SEMVER_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$",
)
_RELEASE_PREFIX = "Release "


class Drift(StrEnum):
    EQUAL = "EQUAL"
    BEHIND = "BEHIND"
    AHEAD = "AHEAD"
    UNKNOWN = "UNKNOWN"


def normalize_version(raw: str) -> str:
    """Trim, strip a leading ``Release `` and a leading ``v``/``V``."""
    value = raw.strip()
    if value.startswith(_RELEASE_PREFIX):
        value = value[len(_RELEASE_PREFIX) :].strip()
    if value[:1] in ("v", "V"):
        value = value[1:]
    return value


def _parse(raw: str) -> tuple[tuple[int, int, int], list[str]] | None:
    match = _SEMVER_RE.match(normalize_version(raw))
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    return (int(major), int(minor), int(patch)), (pre.split(".") if pre else [])


def _cmp_identifier(a: str, b: str) -> int:
    a_num, b_num = a.isdigit(), b.isdigit()
    if a_num and b_num:
        return (int(a) > int(b)) - (int(a) < int(b))
    if a_num:
        return -1
    if b_num:
        return 1
    return (a > b) - (a < b)


def _cmp_pre(a: list[str], b: list[str]) -> int:
    if not a and not b:
        return 0
    if not a:
        return 1  # a release outranks any of its pre-releases
    if not b:
        return -1
    for x, y in zip(a, b, strict=False):
        result = _cmp_identifier(x, y)
        if result:
            return result
    return (len(a) > len(b)) - (len(a) < len(b))


def compare_versions(a: str, b: str) -> int | None:
    """Return -1/0/1 by SemVer precedence, or None if either does not parse."""
    parsed_a, parsed_b = _parse(a), _parse(b)
    if parsed_a is None or parsed_b is None:
        return None
    core_a, pre_a = parsed_a
    core_b, pre_b = parsed_b
    if core_a != core_b:
        return -1 if core_a < core_b else 1
    return _cmp_pre(pre_a, pre_b)


def drift(env_version: str | None, repo_version: str | None) -> Drift:
    """Classify the environment version against the repository version."""
    if not env_version or not repo_version:
        return Drift.UNKNOWN
    result = compare_versions(env_version, repo_version)
    if result is None:
        return Drift.UNKNOWN
    if result == 0:
        return Drift.EQUAL
    return Drift.BEHIND if result < 0 else Drift.AHEAD

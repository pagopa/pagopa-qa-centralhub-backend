"""Standard tag vocabulary of the feature files and the rules on it (pure functions, no DB).

The test type is a feature-level tag; status, flaky and custom tags live in a scenario's
`extra_tags`; polarity is its own field and never appears in `extra_tags`.
"""

from __future__ import annotations

from collections.abc import Iterable

TEST_TYPES: tuple[str, ...] = ("integration", "e2e", "api")
STATUS_TAGS: tuple[str, ...] = ("runnable", "skip", "broken")
FLAKY_TAG = "flaky"
POLARITY_TAGS: tuple[str, ...] = ("positive", "negative")
# Spelling used by the hub before the standard: still read from files, never kept in `extra_tags`.
LEGACY_POLARITY_TAGS: tuple[str, ...] = ("positivo", "negativo")
_POLARITY_NAMES = POLARITY_TAGS + LEGACY_POLARITY_TAGS

TEST_TYPE_MISSING_REASON = "Manca il test-type del file"

# When several status tags are present the safest one wins: nothing runs by mistake.
_STATUS_PRECEDENCE: tuple[str, ...] = ("skip", "broken", "runnable")
DEFAULT_STATUS = "runnable"


def status_reason(tag: str) -> str:
    return f"Lo scenario {tag} deve avere uno e un solo tag tra @runnable, @skip, @broken"


def status_count(extra: Iterable[str]) -> int:
    """How many distinct status tags the scenario carries."""
    return len({t for t in extra if t in STATUS_TAGS})


def normalize_status(extra: Iterable[str]) -> list[str]:
    """Exactly one status tag, no polarity names, no duplicates; the other tags keep their order.

    Precedence `skip` > `broken` > `runnable`; none present gives `runnable`. The kept status
    stays at the position of its first occurrence, a defaulted one goes last.
    """
    given = list(dict.fromkeys(t for t in extra if t not in _POLARITY_NAMES))
    present = [t for t in given if t in STATUS_TAGS]
    chosen = next((s for s in _STATUS_PRECEDENCE if s in present), DEFAULT_STATUS)
    out = [t for t in given if t not in STATUS_TAGS or t == chosen]
    if chosen not in out:
        out.append(chosen)
    return out


def legacy_test_type_tags(extra: Iterable[str]) -> list[str]:
    """Test-type names found among a scenario's tags (they belong to the file)."""
    return [t for t in extra if t in TEST_TYPES]


def default_test_type(repo_folder: str | None) -> str | None:
    """Test type implied by a scope's repository folder (`src/<type>/…`), if any."""
    parts = [p for p in (repo_folder or "").strip().strip("/").split("/") if p]
    if len(parts) >= 2 and parts[0] == "src" and parts[1] in TEST_TYPES:
        return parts[1]
    return None

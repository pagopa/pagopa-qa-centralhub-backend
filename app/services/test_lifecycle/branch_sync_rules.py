"""Pure rules of the working-branch synchronisation (no DB, no network).

A revision with a working branch keeps `work_file_sha`: the git blob sha of the feature file as
it was last pushed to the branch or accepted from it. Comparing it with the blob sha of the hub
render and with the one read from the branch tells who changed what.
"""

from __future__ import annotations

import hashlib
import re

MAX_BRANCH_LEN = 200
_FORBIDDEN = set(" ~^:?*[\\")
# Same characters the Git client accepts in a reference: a name that passes here must not make
# every later call fail with "nome di branch non valido".
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+@/-]{0,199}")
# Shared or special branches the hub must never write to.
_PROTECTED = ("master", "develop", "development", "staging", "production", "prod", "HEAD")
_PROTECTED_PREFIXES = ("refs/", "release/", "hotfix/")


def blob_sha(text: str) -> str:
    """Git blob sha1 of the UTF-8 bytes of `text` (what GitHub reports as the file's `sha`)."""
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 - git object id


def classify(
    hub_sha: str,
    repo_sha: str | None,
    saved_hub_sha: str | None,
    saved_repo_sha: str | None,
    same_content: bool,
) -> str:
    """`in_sync | hub_ahead | repo_ahead | diverged | file_missing` for a branch that exists.

    The hub and the repository are compared each with its own last-synced value: the blob sha of
    the hub render (`saved_hub_sha`) and the one of the file on the branch (`saved_repo_sha`). They
    differ whenever the repository holds the same content in other bytes (line endings, spacing,
    a file accepted from a developer), so one shared value would report changes nobody made.

    `same_content`: the two files are equal once normalised, whatever the shas say (both sides
    changed to the same text is not a divergence). A side with no saved value is assumed changed.
    """
    if repo_sha is None:
        return "file_missing"
    if same_content:
        return "in_sync"
    repo_changed = saved_repo_sha is None or repo_sha != saved_repo_sha
    hub_changed = saved_hub_sha is None or hub_sha != saved_hub_sha
    if repo_changed and hub_changed:
        return "diverged"
    if repo_changed:
        return "repo_ahead"
    if hub_changed:
        return "hub_ahead"
    return "in_sync"


def default_branch_name(code: str, number: int) -> str:
    return f"qa-hub/{code}-r{number}"


def check_branch_name(name: str, base_branch: str) -> str | None:
    """Italian error message when `name` cannot be a working branch, else `None`."""
    value = name or ""
    if not value.strip():
        return "Il nome del branch non può essere vuoto"
    if value == base_branch:
        return f"Il branch non può essere {base_branch}: serve un feature branch"
    if len(value) > MAX_BRANCH_LEN:
        return f"Il nome del branch non può superare {MAX_BRANCH_LEN} caratteri"
    if (
        ".." in value
        or "//" in value
        or "@{" in value
        or value.startswith(("/", "-"))
        or value.endswith(("/", ".", ".lock"))
        or any(ch in _FORBIDDEN or ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        return (
            "Nome del branch non valido: niente spazi, '..', '~^:?*[\\' né '/' in testa o in coda"
        )
    parts = value.split("/")
    if not _REF_RE.fullmatch(value) or any(p.startswith(".") or p.endswith(".lock") for p in parts):
        return (
            "Nome del branch non valido: solo lettere, numeri e . _ + @ / -, "
            "iniziando con un carattere alfanumerico"
        )
    if value in _PROTECTED or value.startswith(_PROTECTED_PREFIXES):
        return "Questo branch è condiviso o riservato: scegli un feature branch"
    return None

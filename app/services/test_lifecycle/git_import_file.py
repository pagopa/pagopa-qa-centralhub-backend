"""Importa da Git: un feature file (qualsiasi branch) nella bozza di una feature.

Il file è solo il punto di partenza: la PR dell'hub lo porterà su `main` nello stesso percorso e,
se il branch di partenza non è `main`, una PR di pulizia lo toglierà da quel branch.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

from app.config import settings

_BLOB = re.compile(r"^/([^/]+/[^/]+)/blob/(.+)$")


class ImportUrlInvalid(Exception):  # noqa: N818
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parse_blob_url(url: str) -> tuple[str, str]:
    """`(repo, resto)` da `https://github.com/<repo>/blob/<branch>/<percorso>`."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in (
        "github.com", "www.github.com"
    ):
        raise ImportUrlInvalid("Serve un link GitHub del tipo https://github.com/…/blob/…")
    m = _BLOB.match(unquote(parsed.path))
    if not m:
        raise ImportUrlInvalid("Il link deve puntare a un file (…/blob/<branch>/<percorso>)")
    repo, rest = m.group(1), m.group(2).strip("/")
    # TL_REPO vuoto (modalità Git locale): si accetta il repository del link
    if settings.tl_repo and repo.lower() != settings.tl_repo.lower():
        raise ImportUrlInvalid(f"Il file deve stare nel repository {settings.tl_repo}")
    if not rest.endswith(".feature"):
        raise ImportUrlInvalid("Il link deve puntare a un file .feature")
    return repo, rest


async def resolve_blob(client: Any, url: str) -> tuple[str, str]:
    """`(branch, percorso)`: i branch possono contenere `/`, si prova dal più lungo."""
    _repo, rest = parse_blob_url(url)
    parts = rest.split("/")
    for i in range(len(parts) - 1, 0, -1):
        branch, path = "/".join(parts[:i]), "/".join(parts[i:])
        if await client.branch_exists(branch):
            return branch, path
    raise ImportUrlInvalid("Branch del link non trovato nel repository")

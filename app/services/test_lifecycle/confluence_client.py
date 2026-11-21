"""Client Confluence per la migrazione del Test Lifecycle (fase 5).

Usa le credenziali Atlassian già configurate (`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`):
REST API v1 di Confluence per le pagine, REST API v3 di Jira per gli utenti.
La scrittura (pannello "Migrata su QA Hub") è possibile solo con
`TL_MIGRATION_CONFLUENCE_WRITE=true`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

MIGRATED_MARKER = "Migrata su QA Hub"


class TlConfluenceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Confluence {status}: {message}")
        self.status = status
        self.message = message


class TlConfluenceNotConfigured(Exception):  # noqa: N818 - simmetrico a TlJiraNotConfigured
    pass


@dataclass(frozen=True)
class PageRef:
    id: str
    title: str
    version: int


@dataclass(frozen=True)
class PageContent:
    id: str
    title: str
    version: int
    storage: str
    url: str


@dataclass(frozen=True)
class AtlassianUser:
    account_id: str
    display_name: str
    email: str | None


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:500] or resp.reason_phrase
    if isinstance(data, dict):
        return str(data.get("message") or data.get("errorMessages") or data)[:500]
    return str(data)[:500]


class TlConfluenceClient:
    page_size = 100

    def __init__(
        self,
        base_url: str,
        email: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base = base_url.rstrip("/")
        self._auth = (email, token)
        self._transport = transport
        self._users: dict[str, AtlassianUser] = {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=30.0) as client:
                resp = await client.request(
                    method,
                    f"{self.base}{path}",
                    params=params,
                    json=json,
                    auth=self._auth,
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise TlConfluenceError(502, f"Confluence non raggiungibile: {exc}") from exc
        if resp.status_code >= 400:
            raise TlConfluenceError(resp.status_code, _error_message(resp))
        data = resp.json() if resp.content else {}
        return data if isinstance(data, dict) else {}

    async def list_feat_pages(self, root_id: str) -> list[PageRef]:
        """Pagine discendenti di `root_id` il cui titolo inizia con `FEAT_`."""
        out: list[PageRef] = []
        start = 0
        while True:
            data = await self._request(
                "GET",
                "/wiki/rest/api/content/search",
                params={
                    "cql": f"ancestor = {int(root_id)} AND type = page ORDER BY title",
                    "expand": "version",
                    "limit": self.page_size,
                    "start": start,
                },
            )
            results = data.get("results") or []
            for r in results:
                title = r.get("title") or ""
                if title.startswith("FEAT_"):
                    version = int((r.get("version") or {}).get("number") or 1)
                    out.append(PageRef(str(r["id"]), title, version))
            if len(results) < self.page_size:
                return out
            start += len(results)

    async def get_page(self, page_id: str) -> PageContent:
        data = await self._request(
            "GET",
            f"/wiki/rest/api/content/{page_id}",
            params={"expand": "body.storage,version"},
        )
        links = data.get("_links") or {}
        base = links.get("base") or f"{self.base}/wiki"
        return PageContent(
            id=str(data["id"]),
            title=data.get("title") or "",
            version=int((data.get("version") or {}).get("number") or 1),
            storage=((data.get("body") or {}).get("storage") or {}).get("value") or "",
            url=f"{base}{links.get('webui', '')}",
        )

    async def get_user(self, account_id: str) -> AtlassianUser:
        cached = self._users.get(account_id)
        if cached is not None:
            return cached
        data = await self._request("GET", "/rest/api/3/user", params={"accountId": account_id})
        user = AtlassianUser(
            account_id=account_id,
            display_name=data.get("displayName") or "",
            email=data.get("emailAddress") or None,
        )
        self._users[account_id] = user
        return user

    async def prepend_panel(self, page: PageContent, panel_html: str) -> int:
        """Mette `panel_html` in cima alla pagina (una volta sola); restituisce la versione."""
        if not settings.tl_migration_confluence_write:
            raise TlConfluenceError(403, "Scrittura su Confluence disattivata")
        if MIGRATED_MARKER in page.storage:
            return page.version
        data = await self._request(
            "PUT",
            f"/wiki/rest/api/content/{page.id}",
            json={
                "id": page.id,
                "type": "page",
                "title": page.title,
                "version": {"number": page.version + 1},
                "body": {
                    "storage": {"value": panel_html + page.storage, "representation": "storage"}
                },
            },
        )
        return int((data.get("version") or {}).get("number") or page.version + 1)


def get_confluence_client() -> TlConfluenceClient:
    if not (settings.jira_base_url and settings.jira_email and settings.jira_api_token):
        raise TlConfluenceNotConfigured("Credenziali Atlassian non configurate")
    return TlConfluenceClient(settings.jira_base_url, settings.jira_email, settings.jira_api_token)

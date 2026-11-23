"""Client Jira in scrittura per il Test Lifecycle (fase 3): issue, link, utenti, ricerche.

Usa le credenziali Jira già configurate (`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`) e la
REST API v3. Ogni errore diventa `TlJiraError` con lo stato e il messaggio di Jira.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

from app.config import settings

StatusCategory = Literal["new", "indeterminate", "done"]
_ISSUE_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]+-[1-9][0-9]*")
_JQL_TEXT_STRIP = re.compile(r'[\\"\'+\-&|!(){}\[\]^~*?:/]')


class TlJiraError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Jira {status}: {message}")
        self.status = status
        self.message = message


class TlJiraNotConfigured(Exception):  # noqa: N818 - simmetrico a TlGitNotConfigured
    pass


@dataclass(frozen=True)
class JiraIssue:
    key: str
    summary: str
    status: str
    status_category: StatusCategory
    issue_type: str
    project: str
    url: str


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:500] or resp.reason_phrase
    if not isinstance(data, dict):
        return str(data)[:500]
    parts = [str(m) for m in data.get("errorMessages") or []]
    parts += [f"{k}: {v}" for k, v in (data.get("errors") or {}).items()]
    return "; ".join(parts) or resp.reason_phrase


def _jql_text(text: str) -> str:
    """Testo libero per `summary ~`: via i caratteri speciali della ricerca Jira."""
    return " ".join(_JQL_TEXT_STRIP.sub(" ", text).split())


class TlJiraClient:
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

    def issue_url(self, key: str) -> str:
        return f"{self.base}/browse/{key}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        ok_statuses: tuple[int, ...] = (),
    ) -> httpx.Response:
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
            raise TlJiraError(502, f"Jira non raggiungibile: {exc}") from exc
        if resp.status_code >= 400 and resp.status_code not in ok_statuses:
            raise TlJiraError(resp.status_code, _error_message(resp))
        return resp

    def _issue(self, data: dict[str, Any]) -> JiraIssue:
        fields = data.get("fields") or {}
        status = fields.get("status") or {}
        category = (status.get("statusCategory") or {}).get("key") or "new"
        if category not in ("new", "indeterminate", "done"):
            category = "new"
        return JiraIssue(
            key=data["key"],
            summary=fields.get("summary") or "",
            status=status.get("name") or "",
            status_category=category,
            issue_type=(fields.get("issuetype") or {}).get("name") or "",
            project=(fields.get("project") or {}).get("key") or data["key"].split("-")[0],
            url=self.issue_url(data["key"]),
        )

    async def get_issue(self, key: str) -> JiraIssue | None:
        # la chiave può arrivare dall'utente: solo una vera chiave Jira entra nell'URL
        if not _ISSUE_KEY_RE.fullmatch(key):
            raise TlJiraError(400, "chiave Jira non valida")
        resp = await self._request(
            "GET",
            f"/rest/api/3/issue/{quote(key)}",
            params={"fields": "summary,status,issuetype,project"},
            ok_statuses=(404,),
        )
        if resp.status_code == 404:
            return None
        return self._issue(resp.json())

    async def find_account_id(self, email: str) -> str | None:
        resp = await self._request("GET", "/rest/api/3/user/search", params={"query": email})
        users = [u for u in resp.json() or [] if u.get("active", True)]
        wanted = email.strip().lower()
        for user in users:
            if (user.get("emailAddress") or "").lower() == wanted:
                return user["accountId"]
        # email nascosta dalla privacy: si accetta solo un risultato univoco
        hidden = [u for u in users if not u.get("emailAddress")]
        return hidden[0]["accountId"] if len(users) == 1 and hidden else None

    async def _search(self, jql: str, limit: int) -> list[JiraIssue]:
        resp = await self._request(
            "POST",
            "/rest/api/3/search/jql",
            json={
                "jql": jql,
                "maxResults": limit,
                "fields": ["summary", "status", "issuetype", "project"],
            },
        )
        return [self._issue(i) for i in resp.json().get("issues", [])]

    async def search_summary(self, project: str, prefix: str) -> JiraIssue | None:
        text = _jql_text(prefix)
        if not text:
            return None
        jql = f'project = "{project}" AND summary ~ "{text}" ORDER BY created DESC'
        for issue in await self._search(jql, 20):
            if issue.summary.startswith(prefix):
                return issue
        return None

    async def search_epics(self, project: str, text: str, limit: int = 20) -> list[JiraIssue]:
        clause = f'project = "{project}" AND issuetype = "{settings.tl_jira_epic_issue_type}"'
        words = _jql_text(text)
        if words:
            clause += f' AND summary ~ "{words}*"'
        return await self._search(f"{clause} ORDER BY updated DESC", limit)

    async def create_issue(
        self,
        *,
        project: str,
        issue_type: str,
        summary: str,
        description_adf: dict[str, Any] | None,
        labels: list[str],
        parent_key: str | None,
        component: str | None,
        assignee_id: str | None,
    ) -> JiraIssue:
        fields: dict[str, Any] = {
            "project": {"key": project},
            "issuetype": {"name": issue_type},
            "summary": summary[:255],
        }
        if description_adf is not None:
            fields["description"] = description_adf
        fields["labels"] = labels
        if parent_key:
            fields["parent"] = {"key": parent_key}
        if component:
            fields["components"] = [{"name": component}]
        if assignee_id:
            fields["assignee"] = {"accountId": assignee_id}
        resp = await self._request("POST", "/rest/api/3/issue", json={"fields": fields})
        key = resp.json()["key"]
        issue = await self.get_issue(key)
        if issue is None:  # appena creata: non dovrebbe succedere
            raise TlJiraError(502, f"Issue {key} creata ma non leggibile")
        return issue

    async def create_link(self, link_type: str, inward_key: str, outward_key: str) -> None:
        await self._request(
            "POST",
            "/rest/api/3/issueLink",
            json={
                "type": {"name": link_type},
                "inwardIssue": {"key": inward_key},
                "outwardIssue": {"key": outward_key},
            },
        )


def get_jira_client() -> TlJiraClient:
    if not (settings.jira_base_url and settings.jira_email and settings.jira_api_token):
        raise TlJiraNotConfigured("Credenziali Jira non configurate")
    return TlJiraClient(settings.jira_base_url, settings.jira_email, settings.jira_api_token)

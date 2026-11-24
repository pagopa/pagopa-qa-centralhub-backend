# ruff: noqa: S101, S106
"""Client Jira in scrittura del Test Lifecycle (fase 3)."""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import settings
from app.services.test_lifecycle import jira_client
from app.services.test_lifecycle.jira_client import TlJiraClient, TlJiraError
from app.services.test_lifecycle.jira_client import get_jira_client as real_get_jira_client

BASE = "https://example.atlassian.net"


def _issue(key="PQ-1", status_cat="new", status="Backlog", itype="Epic", project="PQ", summary="S"):
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"name": status, "statusCategory": {"key": status_cat}},
            "issuetype": {"name": itype},
            "project": {"key": project},
        },
    }


def _client(handler) -> tuple[TlJiraClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return TlJiraClient(BASE, "me@example.test", "tok", transport=httpx.MockTransport(wrapped)), seen


async def test_get_issue_maps_status_category():
    client, seen = _client(lambda r: httpx.Response(200, json=_issue("PQ-7", "indeterminate", "In Progress")))
    issue = await client.get_issue("PQ-7")
    assert (issue.key, issue.status, issue.status_category, issue.issue_type, issue.project) == (
        "PQ-7", "In Progress", "indeterminate", "Epic", "PQ",
    )
    assert issue.url == f"{BASE}/browse/PQ-7"
    assert seen[0].url.path == "/rest/api/3/issue/PQ-7"
    assert seen[0].headers["Authorization"].startswith("Basic ")


async def test_get_issue_404_returns_none():
    client, _ = _client(lambda r: httpx.Response(404, json={"errorMessages": ["Issue does not exist"]}))
    assert await client.get_issue("PQ-999999") is None


async def test_find_account_id_matches_email_exactly():
    users = [
        {"accountId": "a1", "emailAddress": "other@example.test", "active": True},
        {"accountId": "a2", "emailAddress": "Mario.Rossi@example.test", "active": True},
    ]
    client, seen = _client(lambda r: httpx.Response(200, json=users))
    assert await client.find_account_id("mario.rossi@example.test") == "a2"
    assert seen[0].url.params["query"] == "mario.rossi@example.test"


async def test_find_account_id_single_hidden_email_and_none():
    client, _ = _client(lambda r: httpx.Response(200, json=[{"accountId": "a9", "active": True}]))
    assert await client.find_account_id("x@example.test") == "a9"
    client, _ = _client(lambda r: httpx.Response(200, json=[]))
    assert await client.find_account_id("x@example.test") is None


async def test_create_issue_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"id": "1", "key": "PQ-900"})
        return httpx.Response(200, json=_issue("PQ-900", itype="Progettazione Scenari e casi di test"))

    client, seen = _client(handler)
    created = await client.create_issue(
        project="PQ",
        issue_type="Progettazione Scenari e casi di test",
        summary="[FEAT_001_FdR3] Progettazione — Pagamento",
        description_adf={"type": "doc", "version": 1, "content": []},
        labels=["testing"],
        parent_key="PQ-495",
        component="FDR",
        assignee_id="acc-1",
    )
    assert created.key == "PQ-900"
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/rest/api/3/issue"
    assert body["fields"] == {
        "project": {"key": "PQ"},
        "issuetype": {"name": "Progettazione Scenari e casi di test"},
        "summary": "[FEAT_001_FdR3] Progettazione — Pagamento",
        "description": {"type": "doc", "version": 1, "content": []},
        "labels": ["testing"],
        "parent": {"key": "PQ-495"},
        "components": [{"name": "FDR"}],
        "assignee": {"accountId": "acc-1"},
    }


async def test_create_epic_without_parent():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"key": "PQ-901"})
        return httpx.Response(200, json=_issue("PQ-901"))

    client, seen = _client(handler)
    await client.create_issue(
        project="PQ", issue_type="Epic", summary="[QA Epic] FdR", description_adf=None,
        labels=["testing"], parent_key=None, component=None, assignee_id=None,
    )
    fields = json.loads(seen[0].content)["fields"]
    assert "parent" not in fields and "components" not in fields and "assignee" not in fields
    assert "description" not in fields


async def test_create_link_payload():
    client, seen = _client(lambda r: httpx.Response(201))
    await client.create_link("Relates", "PQ-900", "CHK-12")
    assert seen[0].url.path == "/rest/api/3/issueLink"
    assert json.loads(seen[0].content) == {
        "type": {"name": "Relates"},
        "inwardIssue": {"key": "PQ-900"},
        "outwardIssue": {"key": "CHK-12"},
    }


async def test_search_summary_prefix():
    issues = [
        _issue("PQ-5", summary="[FEAT_001_FdR3] Progettazione — Altro titolo"),
        _issue("PQ-6", summary="Nota su [FEAT_001_FdR3] Progettazione"),
    ]
    client, seen = _client(lambda r: httpx.Response(200, json={"issues": issues, "isLast": True}))
    found = await client.search_summary("PQ", "[FEAT_001_FdR3] Progettazione")
    assert found.key == "PQ-5"
    jql = json.loads(seen[0].content)["jql"]
    assert jql.startswith('project = "PQ" AND summary ~ ')
    client, _ = _client(lambda r: httpx.Response(200, json={"issues": [issues[1]], "isLast": True}))
    assert await client.search_summary("PQ", "[FEAT_001_FdR3] Progettazione") is None


async def test_search_epics_jql_and_escaping():
    client, seen = _client(lambda r: httpx.Response(200, json={"issues": [_issue("PQ-495")], "isLast": True}))
    epics = await client.search_epics("PQ", 'FdR "x" (e2e)')
    assert [e.key for e in epics] == ["PQ-495"]
    jql = json.loads(seen[0].content)["jql"]
    assert 'issuetype = "Epic"' in jql and "ORDER BY updated DESC" in jql
    assert '"x"' not in jql and "(" not in jql.split("ORDER")[0].split("~")[-1]
    client, seen = _client(lambda r: httpx.Response(200, json={"issues": [], "isLast": True}))
    await client.search_epics("PQ", "")
    assert "summary ~" not in json.loads(seen[0].content)["jql"]


async def test_error_carries_jira_message():
    body = {"errorMessages": [], "errors": {"parent": "Issue PQ-1 is not an epic"}}
    client, _ = _client(lambda r: httpx.Response(400, json=body))
    with pytest.raises(TlJiraError) as exc:
        await client.create_link("Relates", "PQ-1", "PQ-2")
    assert exc.value.status == 400
    assert "not an epic" in exc.value.message


async def test_network_error_is_502():
    def boom(request):
        raise httpx.ConnectError("down")

    client, _ = _client(boom)
    with pytest.raises(TlJiraError) as exc:
        await client.get_issue("PQ-1")
    assert exc.value.status == 502


def test_not_configured_raises(monkeypatch):
    # il conftest sostituisce get_jira_client: qui si prova quella reale
    monkeypatch.setattr(jira_client, "get_jira_client", real_get_jira_client)
    monkeypatch.setattr(settings, "jira_base_url", "")
    with pytest.raises(jira_client.TlJiraNotConfigured):
        jira_client.get_jira_client()
    monkeypatch.setattr(settings, "jira_base_url", BASE)
    monkeypatch.setattr(settings, "jira_email", "a@b.c")
    monkeypatch.setattr(settings, "jira_api_token", "t")
    assert isinstance(jira_client.get_jira_client(), TlJiraClient)


@pytest.mark.parametrize(
    "key",
    [
        "../../myself", "PQ-1/../../user", "pq-1", "PQ-1?x=1", "",
        "PQ-01", "P-1", "A" * 5000, "PQ-" + "1" * 40,
    ],
)
async def test_get_issue_rejects_keys_that_are_not_issue_keys(key):
    c, seen = _client(lambda req: httpx.Response(200, json=_issue()))
    with pytest.raises(TlJiraError) as exc:
        await c.get_issue(key)
    assert exc.value.status == 400
    assert seen == []


@pytest.mark.parametrize("key", ["PQ-1", "PQ-857", "AB_2-10", "X9-1"])
async def test_get_issue_accepts_issue_keys(key):
    c, seen = _client(lambda req: httpx.Response(200, json=_issue(key=key)))
    assert (await c.get_issue(key)).key == key
    assert seen[0].url.path == f"/rest/api/3/issue/{key}"


async def test_delete_issue_sends_delete_without_subtasks():
    client, seen = _client(lambda r: httpx.Response(204))
    await client.delete_issue("PQ-12")
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == "/rest/api/3/issue/PQ-12"
    assert seen[0].url.params["deleteSubtasks"] == "false"


async def test_delete_issue_treats_404_as_success():
    client, _ = _client(lambda r: httpx.Response(404, json={"errorMessages": ["gone"]}))
    await client.delete_issue("PQ-12")


async def test_delete_issue_raises_on_403():
    client, _ = _client(lambda r: httpx.Response(403, json={"errorMessages": ["forbidden"]}))
    with pytest.raises(TlJiraError) as exc:
        await client.delete_issue("PQ-12")
    assert exc.value.status == 403


async def test_delete_issue_rejects_invalid_key():
    client, seen = _client(lambda r: httpx.Response(204))
    with pytest.raises(TlJiraError) as exc:
        await client.delete_issue("not a key")
    assert exc.value.status == 400
    assert seen == []

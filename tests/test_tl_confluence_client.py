# ruff: noqa: S101, S106
"""Fase 5, Task 3: client Confluence della migrazione."""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import settings
from app.services.test_lifecycle import confluence_client as cc
from app.services.test_lifecycle.confluence_client import (
    PageContent,
    TlConfluenceClient,
    TlConfluenceError,
)
from app.services.test_lifecycle.confluence_client import (
    get_confluence_client as real_get_confluence_client,
)

BASE = "https://example.atlassian.net"


def _client(handler):
    seen: list[httpx.Request] = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)
    return TlConfluenceClient(BASE, "me@example.test", "tok", transport=transport), seen


def _page(i, title, version=1):
    return {"id": str(i), "title": title, "version": {"number": version}}


async def test_list_feat_pages_paginates_and_filters():
    def handler(request):
        start = int(request.url.params.get("start", "0"))
        if start == 0:
            results = [_page(1, "FEAT_001_X - Uno"), _page(2, "Guida")]
            return httpx.Response(200, json={"results": results, "size": 2, "limit": 2, "start": 0})
        return httpx.Response(200, json={"results": [_page(3, "FEAT_002_X - Due", 4)], "size": 1,
                                         "limit": 2, "start": 2})

    client, seen = _client(handler)
    client.page_size = 2
    pages = await client.list_feat_pages("2815950851")
    assert [(p.id, p.title, p.version) for p in pages] == [("1", "FEAT_001_X - Uno", 1),
                                                           ("3", "FEAT_002_X - Due", 4)]
    assert seen[0].url.path == "/wiki/rest/api/content/search"
    assert "ancestor = 2815950851" in seen[0].url.params["cql"]


async def test_get_page():
    body = {
        "id": "7", "title": "FEAT_001_X", "version": {"number": 3},
        "body": {"storage": {"value": "<p>ciao</p>"}},
        "_links": {"webui": "/spaces/QDP/pages/7/FEAT", "base": f"{BASE}/wiki"},
    }
    client, seen = _client(lambda r: httpx.Response(200, json=body))
    page = await client.get_page("7")
    url = f"{BASE}/wiki/spaces/QDP/pages/7/FEAT"
    assert page == PageContent("7", "FEAT_001_X", 3, "<p>ciao</p>", url)
    assert seen[0].url.params["expand"] == "body.storage,version"


async def test_get_user_hidden_email_and_cache():
    user_json = {"accountId": "a1", "displayName": "Anna"}
    client, seen = _client(lambda r: httpx.Response(200, json=user_json))
    user = await client.get_user("a1")
    assert (user.account_id, user.display_name, user.email) == ("a1", "Anna", None)
    await client.get_user("a1")
    assert len(seen) == 1 and seen[0].url.path == "/rest/api/3/user"


async def test_prepend_panel(monkeypatch):
    monkeypatch.setattr(settings, "tl_migration_confluence_write", True)
    client, seen = _client(lambda r: httpx.Response(200, json={"version": {"number": 4}}))
    page = PageContent("7", "T", 3, "<p>old</p>", "u")
    assert await client.prepend_panel(page, "<p>Migrata su QA Hub</p>") == 4
    sent = json.loads(seen[0].content)
    assert seen[0].method == "PUT" and sent["version"]["number"] == 4
    assert sent["body"]["storage"]["value"] == "<p>Migrata su QA Hub</p><p>old</p>"
    # già presente: nessuna nuova scrittura
    marked = PageContent("7", "T", 4, "<p>Migrata su QA Hub</p><p>old</p>", "u")
    assert await client.prepend_panel(marked, "<p>Migrata su QA Hub</p>") == 4
    assert len(seen) == 1


async def test_prepend_panel_conflict_and_disabled(monkeypatch):
    monkeypatch.setattr(settings, "tl_migration_confluence_write", True)
    client, _ = _client(lambda r: httpx.Response(409, json={"message": "version conflict"}))
    with pytest.raises(TlConfluenceError) as exc:
        await client.prepend_panel(PageContent("7", "T", 3, "", "u"), "<p>Migrata su QA Hub</p>")
    assert exc.value.status == 409
    monkeypatch.setattr(settings, "tl_migration_confluence_write", False)
    client, seen = _client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(TlConfluenceError) as exc:
        await client.prepend_panel(PageContent("7", "T", 3, "", "u"), "<p>Migrata su QA Hub</p>")
    assert exc.value.status == 403 and seen == []


async def test_network_error_is_502():
    def boom(request):
        raise httpx.ConnectError("down")

    client, _ = _client(boom)
    with pytest.raises(TlConfluenceError) as exc:
        await client.get_page("1")
    assert exc.value.status == 502


def test_not_configured(monkeypatch):
    monkeypatch.setattr(cc, "get_confluence_client", real_get_confluence_client)
    monkeypatch.setattr(settings, "jira_base_url", "")
    with pytest.raises(cc.TlConfluenceNotConfigured):
        cc.get_confluence_client()

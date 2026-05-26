from __future__ import annotations

import base64
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.github import GitHubClient


@pytest.mark.anyio
async def test_list_directory_passes_auth_header() -> None:
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = [{"name": "wisp-tests", "type": "dir"}]

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response) as mock_get:
        client = GitHubClient(token="test-token", repo="org/repo")
        result = await client.list_directory("wisp-tests")

    call_kwargs = mock_get.call_args
    assert "Authorization" in call_kwargs.kwargs["headers"]
    assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert result == [{"name": "wisp-tests", "type": "dir"}]


@pytest.mark.anyio
async def test_get_file_content_decodes_base64() -> None:
    raw = json.dumps({"passed": 10}).encode()
    encoded = base64.b64encode(raw).decode()

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"content": encoded, "encoding": "base64"}

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
        client = GitHubClient(token="test-token", repo="org/repo")
        content = await client.get_file_content("some/path/stats.json")

    assert json.loads(content) == {"passed": 10}

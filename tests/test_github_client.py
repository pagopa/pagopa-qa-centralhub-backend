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


@pytest.mark.anyio
async def test_list_workflow_runs_filters_by_success_status() -> None:
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "workflow_runs": [{"id": 123, "created_at": "2026-06-09T03:00:00Z"}]
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response) as mock_get:
        client = GitHubClient(token="test-token", repo="pagopa/pagopa-qa")
        result = await client.list_workflow_runs("gpd_report.yml")

    call_kwargs = mock_get.call_args
    assert call_kwargs.kwargs["params"]["status"] == "success"
    assert "actions/workflows/gpd_report.yml/runs" in str(mock_get.call_args.args[0])
    assert result == [{"id": 123, "created_at": "2026-06-09T03:00:00Z"}]


@pytest.mark.anyio
async def test_get_job_log_fetches_first_job_logs() -> None:
    jobs_response = MagicMock()
    jobs_response.raise_for_status = MagicMock()
    jobs_response.json.return_value = {"jobs": [{"id": 999, "name": "report"}]}

    log_response = MagicMock()
    log_response.raise_for_status = MagicMock()
    log_response.text = "report data {'TOTAL': 1}"

    with patch(
        "httpx.AsyncClient.get",
        new_callable=AsyncMock,
        side_effect=[jobs_response, log_response],
    ) as mock_get:
        client = GitHubClient(token="test-token", repo="pagopa/pagopa-qa")
        result = await client.get_job_log(123)

    assert result == "report data {'TOTAL': 1}"
    assert "actions/runs/123/jobs" in str(mock_get.call_args_list[0].args[0])
    assert "actions/jobs/999/logs" in str(mock_get.call_args_list[1].args[0])
    assert mock_get.call_args_list[1].kwargs["follow_redirects"] is True


@pytest.mark.anyio
async def test_get_job_log_returns_empty_string_when_no_jobs() -> None:
    jobs_response = MagicMock()
    jobs_response.raise_for_status = MagicMock()
    jobs_response.json.return_value = {"jobs": []}

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=jobs_response):
        client = GitHubClient(token="test-token", repo="pagopa/pagopa-qa")
        result = await client.get_job_log(123)

    assert result == ""

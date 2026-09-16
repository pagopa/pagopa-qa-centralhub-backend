# ruff: noqa: S101, S106

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.github import GitHubClient


@pytest.mark.anyio
async def test_list_directory_passes_auth_header() -> None:
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = [{"name": "wisp-tests", "type": "dir"}]

    with patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response
    ) as mock_get:
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

    with patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response
    ) as mock_get:
        client = GitHubClient(token="test-token", repo="pagopa/pagopa-qa")
        result = await client.list_workflow_runs("gpd_report.yml")

    call_kwargs = mock_get.call_args
    assert call_kwargs.kwargs["params"]["status"] == "success"
    assert "actions/workflows/gpd_report.yml/runs" in str(mock_get.call_args.args[0])
    assert result == [{"id": 123, "created_at": "2026-06-09T03:00:00Z"}]


@pytest.mark.anyio
async def test_list_workflow_runs_accepts_completed_status() -> None:
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"workflow_runs": [{"id": 456}]}

    with patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response
    ) as mock_get:
        client = GitHubClient(token="test-token", repo="pagopa/pagopa-qa")
        result = await client.list_workflow_runs(
            "sanp.yml", status="completed", per_page=25, page=2
        )

    assert mock_get.call_args.kwargs["params"] == {
        "status": "completed",
        "per_page": 25,
        "page": 2,
    }
    assert result == [{"id": 456}]


@pytest.mark.anyio
async def test_list_run_artifacts() -> None:
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"artifacts": [{"id": 789, "name": "drift-d-api"}]}

    with patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response
    ) as mock_get:
        client = GitHubClient(token="test-token", repo="pagopa/pagopa-qa")
        result = await client.list_run_artifacts(456, per_page=25, page=2)

    assert "repos/pagopa/pagopa-qa/actions/runs/456/artifacts" in str(
        mock_get.call_args.args[0]
    )
    assert mock_get.call_args.kwargs["params"] == {"per_page": 25, "page": 2}
    assert result == [{"id": 789, "name": "drift-d-api"}]


@pytest.mark.anyio
async def test_download_artifact_follows_redirects() -> None:
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.headers = {"Content-Length": "7"}

    async def chunks():
        yield b"zip"
        yield b"data"

    mock_response.aiter_bytes = chunks
    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(return_value=mock_response)
    stream_context.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient.stream", return_value=stream_context) as mock_stream:
        client = GitHubClient(token="test-token", repo="pagopa/pagopa-qa")
        result = await client.download_artifact(789, max_bytes=10)

    assert result == b"zipdata"
    assert mock_stream.call_args.args[:2] == (
        "GET",
        "https://api.github.com/repos/pagopa/pagopa-qa/actions/artifacts/789/zip",
    )
    assert mock_stream.call_args.kwargs["follow_redirects"] is True


@pytest.mark.anyio
async def test_download_artifact_rejects_oversized_content_length() -> None:
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.headers = {"Content-Length": "11"}
    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(return_value=mock_response)
    stream_context.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient.stream", return_value=stream_context):
        client = GitHubClient(token="test-token", repo="pagopa/pagopa-qa")
        with pytest.raises(ValueError, match="maximum size"):
            await client.download_artifact(789, max_bytes=10)


@pytest.mark.anyio
async def test_download_artifact_rejects_oversized_streamed_content() -> None:
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.headers = {}

    async def chunks():
        yield b"123456"
        yield b"78901"

    mock_response.aiter_bytes = chunks
    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(return_value=mock_response)
    stream_context.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient.stream", return_value=stream_context):
        client = GitHubClient(token="test-token", repo="pagopa/pagopa-qa")
        with pytest.raises(ValueError, match="maximum size"):
            await client.download_artifact(789, max_bytes=10)


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
async def test_get_job_log_concatenates_logs_from_all_jobs() -> None:
    jobs_response = MagicMock()
    jobs_response.raise_for_status = MagicMock()
    jobs_response.json.return_value = {
        "jobs": [
            {"id": 111, "name": "Create Runner"},
            {"id": 222, "name": "Report GPD APD prod"},
            {"id": 333, "name": "Cleanup Runner"},
        ]
    }

    log_responses = []
    for text in ["create runner log", "report data {'TOTAL': 1}", "cleanup log"]:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.text = text
        log_responses.append(resp)

    with patch(
        "httpx.AsyncClient.get",
        new_callable=AsyncMock,
        side_effect=[jobs_response, *log_responses],
    ) as mock_get:
        client = GitHubClient(token="test-token", repo="pagopa/pagopa-qa")
        result = await client.get_job_log(123)

    assert "report data {'TOTAL': 1}" in result
    assert "actions/jobs/111/logs" in str(mock_get.call_args_list[1].args[0])
    assert "actions/jobs/222/logs" in str(mock_get.call_args_list[2].args[0])
    assert "actions/jobs/333/logs" in str(mock_get.call_args_list[3].args[0])


@pytest.mark.anyio
async def test_get_job_log_returns_empty_string_when_no_jobs() -> None:
    jobs_response = MagicMock()
    jobs_response.raise_for_status = MagicMock()
    jobs_response.json.return_value = {"jobs": []}

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=jobs_response):
        client = GitHubClient(token="test-token", repo="pagopa/pagopa-qa")
        result = await client.get_job_log(123)

    assert result == ""

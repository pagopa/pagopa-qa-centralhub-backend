from __future__ import annotations

import base64

import httpx
import structlog

logger = structlog.get_logger(__name__)


class GitHubClient:
    BASE_URL = "https://api.github.com"

    def __init__(self, token: str, repo: str) -> None:
        try:
            self.token = token
            self.repo = repo
        except Exception:
            logger.exception("GitHubClient.__init__ failed")
            raise

    def _headers(self) -> dict[str, str]:
        try:
            return {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        except Exception:
            logger.exception("GitHubClient._headers failed")
            raise

    async def list_directory(self, path: str, ref: str = "gh-pages") -> list[dict]:
        try:
            url = f"{self.BASE_URL}/repos/{self.repo}/contents/{path}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url, params={"ref": ref}, headers=self._headers(), timeout=30.0
                )
                resp.raise_for_status()
                _query_result_for_logging = resp.json()  # type: ignore[return-value]
                if _query_result_for_logging is None or not _query_result_for_logging:
                    logger.info("GitHubClient.list_directory query succeeded with 0 results")
                return _query_result_for_logging
        except Exception:
            logger.exception("GitHubClient.list_directory failed")
            raise

    async def get_file_content(self, path: str, ref: str = "gh-pages") -> bytes:
        try:
            url = f"{self.BASE_URL}/repos/{self.repo}/contents/{path}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url, params={"ref": ref}, headers=self._headers(), timeout=30.0
                )
                resp.raise_for_status()
                data = resp.json()
                if data is None or not data:
                    logger.info("GitHubClient.get_file_content query succeeded with 0 results")
                return base64.b64decode(data["content"].replace("\n", ""))
        except Exception:
            logger.exception("GitHubClient.get_file_content failed")
            raise

    async def list_workflow_runs(
        self,
        workflow_file: str,
        *,
        status: str = "success",
        per_page: int = 100,
        page: int = 1,
    ) -> list[dict]:
        try:
            url = f"{self.BASE_URL}/repos/{self.repo}/actions/workflows/{workflow_file}/runs"
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    params={"status": status, "per_page": per_page, "page": page},
                    headers=self._headers(),
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
                if data is None or not data:
                    logger.info("GitHubClient.list_workflow_runs query succeeded with 0 results")
                _query_result_for_logging = data.get("workflow_runs", [])
                if _query_result_for_logging is None or not _query_result_for_logging:
                    logger.info("GitHubClient.list_workflow_runs query succeeded with 0 results")
                return _query_result_for_logging
        except Exception:
            logger.exception("GitHubClient.list_workflow_runs failed")
            raise

    async def list_run_artifacts(
        self, run_id: int, *, per_page: int = 100, page: int = 1
    ) -> list[dict]:
        try:
            url = f"{self.BASE_URL}/repos/{self.repo}/actions/runs/{run_id}/artifacts"
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    params={"per_page": per_page, "page": page},
                    headers=self._headers(),
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
                if data is None or not data:
                    logger.info("GitHubClient.list_run_artifacts query succeeded with 0 results")
                _query_result_for_logging = data.get("artifacts", [])
                if _query_result_for_logging is None or not _query_result_for_logging:
                    logger.info("GitHubClient.list_run_artifacts query succeeded with 0 results")
                return _query_result_for_logging
        except Exception:
            logger.exception("GitHubClient.list_run_artifacts failed")
            raise

    async def download_artifact(self, artifact_id: int, *, max_bytes: int) -> bytes:
        try:
            url = f"{self.BASE_URL}/repos/{self.repo}/actions/artifacts/{artifact_id}/zip"
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "GET",
                    url,
                    headers=self._headers(),
                    timeout=30.0,
                    follow_redirects=True,
                ) as resp:
                    resp.raise_for_status()
                    content_length = resp.headers.get("Content-Length")
                    if content_length is not None and int(content_length) > max_bytes:
                        raise ValueError("Artifact exceeds maximum size")

                    content = bytearray()
                    async for chunk in resp.aiter_bytes():
                        if len(content) + len(chunk) > max_bytes:
                            raise ValueError("Artifact exceeds maximum size")
                        content.extend(chunk)
                    if not content:
                        logger.info("GitHubClient.download_artifact query succeeded with 0 results")
                    return bytes(content)
        except Exception:
            logger.exception("GitHubClient.download_artifact failed")
            raise

    async def get_job_log(self, run_id: int) -> str:
        try:
            async with httpx.AsyncClient() as client:
                jobs_resp = await client.get(
                    f"{self.BASE_URL}/repos/{self.repo}/actions/runs/{run_id}/jobs",
                    headers=self._headers(),
                    timeout=30.0,
                )
                jobs_resp.raise_for_status()
                jobs = jobs_resp.json().get("jobs", [])
                if not jobs:
                    logger.info("GitHubClient.get_job_log query succeeded with 0 results")
                    return ""

                logs = []
                for job in jobs:
                    log_resp = await client.get(
                        f"{self.BASE_URL}/repos/{self.repo}/actions/jobs/{job['id']}/logs",
                        headers=self._headers(),
                        timeout=30.0,
                        follow_redirects=True,
                    )
                    log_resp.raise_for_status()
                    logs.append(log_resp.text)
                content = "\n".join(logs)
                if not content:
                    logger.info("GitHubClient.get_job_log query succeeded with 0 results")
                return content
        except Exception:
            logger.exception("GitHubClient.get_job_log failed")
            raise

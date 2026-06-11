from __future__ import annotations

import base64

import httpx


class GitHubClient:
    BASE_URL = "https://api.github.com"

    def __init__(self, token: str, repo: str) -> None:
        self.token = token
        self.repo = repo

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def list_directory(self, path: str, ref: str = "gh-pages") -> list[dict]:
        url = f"{self.BASE_URL}/repos/{self.repo}/contents/{path}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url, params={"ref": ref}, headers=self._headers(), timeout=30.0
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[return-value]

    async def get_file_content(self, path: str, ref: str = "gh-pages") -> bytes:
        url = f"{self.BASE_URL}/repos/{self.repo}/contents/{path}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url, params={"ref": ref}, headers=self._headers(), timeout=30.0
            )
            resp.raise_for_status()
            data = resp.json()
            return base64.b64decode(data["content"].replace("\n", ""))

    async def list_workflow_runs(self, workflow_file: str, per_page: int = 100, page: int = 1) -> list[dict]:
        url = f"{self.BASE_URL}/repos/{self.repo}/actions/workflows/{workflow_file}/runs"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params={"status": "success", "per_page": per_page, "page": page},
                headers=self._headers(),
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("workflow_runs", [])

    async def get_job_log(self, run_id: int) -> str:
        async with httpx.AsyncClient() as client:
            jobs_resp = await client.get(
                f"{self.BASE_URL}/repos/{self.repo}/actions/runs/{run_id}/jobs",
                headers=self._headers(),
                timeout=30.0,
            )
            jobs_resp.raise_for_status()
            jobs = jobs_resp.json().get("jobs", [])
            if not jobs:
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
            return "\n".join(logs)

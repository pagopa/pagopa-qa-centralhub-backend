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

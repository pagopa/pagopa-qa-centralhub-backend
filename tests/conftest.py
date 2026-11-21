from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _no_real_jira(monkeypatch):
    """Nessun test parla con il Jira reale (il .env locale può contenere credenziali vere):
    il client Jira del Test Lifecycle risulta non configurato, salvo test che installano
    il finto con tests.tl_fakes.install_fake_jira."""
    from app.config import settings
    from tests.tl_fakes import uninstall_jira

    uninstall_jira(monkeypatch)
    # Il .env di sviluppo può attivare la modalità Git locale: i test partono sempre da GitHub.
    monkeypatch.setattr(settings, "tl_git_mode", "github")
    monkeypatch.setattr(settings, "tl_local_repo_path", "")

from __future__ import annotations

import base64
import hashlib
import uuid

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.bdd import BddProject, BddScenario, BddSettings


def _fernet() -> Fernet:
    # Derive a valid 32-byte Fernet key from the configured encryption_key
    raw = hashlib.sha256(settings.encryption_key.encode()).digest()
    key = base64.urlsafe_b64encode(raw)
    return Fernet(key)


def encrypt_value(value: str | None) -> str | None:
    if value is None:
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt_value(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None


# ── Projects ──────────────────────────────────────────────────────────────────

async def list_projects(db: AsyncSession) -> list[tuple[BddProject, int]]:
    """Returns list of (project, scenario_count) tuples."""
    rows = await db.execute(
        select(BddProject, func.count(BddScenario.id).label("cnt"))
        .outerjoin(BddScenario, BddScenario.project_id == BddProject.id)
        .group_by(BddProject.id)
        .order_by(BddProject.created_at.desc())
    )
    return [(row.BddProject, row.cnt) for row in rows]


async def get_project(db: AsyncSession, project_id: uuid.UUID) -> BddProject | None:
    return await db.get(BddProject, project_id)


async def create_project(db: AsyncSession, name: str, description: str | None) -> BddProject:
    project = BddProject(name=name, description=description)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def update_project(db: AsyncSession, project: BddProject, fields: dict) -> BddProject:
    for k, v in fields.items():
        setattr(project, k, v)
    await db.commit()
    await db.refresh(project)
    return project


async def delete_project(db: AsyncSession, project: BddProject) -> None:
    await db.delete(project)
    await db.commit()


# ── Scenarios ─────────────────────────────────────────────────────────────────

async def list_scenarios(
    db: AsyncSession,
    project_id: uuid.UUID | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[BddScenario], int]:
    q = select(BddScenario).order_by(BddScenario.created_at.desc())
    if project_id is not None:
        q = q.where(BddScenario.project_id == project_id)
    if status is not None:
        q = q.where(BddScenario.status == status)

    total_q = select(func.count()).select_from(q.subquery())
    total = (await db.scalar(total_q)) or 0

    q = q.offset((page - 1) * page_size).limit(page_size)
    scenarios = list(await db.scalars(q))
    return scenarios, total


async def get_scenario(db: AsyncSession, scenario_id: uuid.UUID) -> BddScenario | None:
    return await db.get(BddScenario, scenario_id)


async def create_scenario(db: AsyncSession, **kwargs) -> BddScenario:
    tags = kwargs.pop("tags", None) or []
    scenario = BddScenario(**kwargs, tags=tags)
    db.add(scenario)
    await db.commit()
    await db.refresh(scenario)
    return scenario


async def update_scenario(db: AsyncSession, scenario: BddScenario, fields: dict) -> BddScenario:
    for k, v in fields.items():
        setattr(scenario, k, v)
    await db.commit()
    await db.refresh(scenario)
    return scenario


async def delete_scenario(db: AsyncSession, scenario: BddScenario) -> None:
    await db.delete(scenario)
    await db.commit()


# ── Settings ──────────────────────────────────────────────────────────────────

async def get_settings(db: AsyncSession) -> BddSettings:
    s = await db.get(BddSettings, 1)
    if s is None:
        s = BddSettings(id=1)
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return s


async def update_settings(db: AsyncSession, fields: dict) -> BddSettings:
    s = await get_settings(db)
    # Encrypt sensitive fields before persisting
    if "claude_api_key" in fields and fields["claude_api_key"]:
        fields["claude_api_key"] = encrypt_value(fields["claude_api_key"])
    if "confluence_api_token" in fields and fields["confluence_api_token"]:
        fields["confluence_api_token"] = encrypt_value(fields["confluence_api_token"])
    for k, v in fields.items():
        setattr(s, k, v)
    await db.commit()
    await db.refresh(s)
    return s


def settings_to_out(s: BddSettings) -> dict:
    """Returns dict for SettingsOut — masks encrypted fields."""
    return {
        "ai_provider": s.ai_provider,
        "claude_api_key_set": bool(s.claude_api_key),
        "claude_model": s.claude_model,
        "ollama_base_url": s.ollama_base_url,
        "ollama_model": s.ollama_model,
        "confluence_email": s.confluence_email,
        "confluence_token_set": bool(s.confluence_api_token),
        "gherkin_language": s.gherkin_language,
        "max_scenarios": s.max_scenarios,
    }


def get_decrypted_settings(s: BddSettings) -> dict:
    """Returns settings with decrypted credentials for use in AI/parser services."""
    return {
        "ai_provider": s.ai_provider,
        "claude_api_key": decrypt_value(s.claude_api_key),
        "claude_model": s.claude_model,
        "ollama_base_url": s.ollama_base_url,
        "ollama_model": s.ollama_model,
        "confluence_email": s.confluence_email,
        "confluence_api_token": decrypt_value(s.confluence_api_token),
        "gherkin_language": s.gherkin_language,
        "max_scenarios": s.max_scenarios,
    }

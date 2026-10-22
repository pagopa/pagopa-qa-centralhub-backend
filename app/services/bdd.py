from __future__ import annotations

import base64
import hashlib
import uuid

import structlog
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.bdd import BddProject, BddScenario, BddSettings

logger = structlog.get_logger(__name__)


def _fernet() -> Fernet:
    try:
        # Derive a valid 32-byte Fernet key from the configured encryption_key
        raw = hashlib.sha256(settings.encryption_key.encode()).digest()
        key = base64.urlsafe_b64encode(raw)
        return Fernet(key)
    except Exception:
        logger.exception("_fernet failed")
        raise


def encrypt_value(value: str | None) -> str | None:
    try:
        if value is None:
            return None
        return _fernet().encrypt(value.encode()).decode()
    except Exception:
        logger.exception("encrypt_value failed")
        raise


def decrypt_value(value: str | None) -> str | None:
    try:
        if value is None:
            return None
        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            logger.exception("decrypt_value failed")
            return None
    except Exception:
        logger.exception("decrypt_value failed")
        raise


# ── Projects ──────────────────────────────────────────────────────────────────

async def list_projects(db: AsyncSession) -> list[tuple[BddProject, int]]:
    """Returns list of (project, scenario_count) tuples."""
    try:
        rows = await db.execute(
            select(BddProject, func.count(BddScenario.id).label("cnt"))
            .outerjoin(BddScenario, BddScenario.project_id == BddProject.id)
            .group_by(BddProject.id)
            .order_by(BddProject.created_at.desc())
        )
        _query_result_for_logging = [(row.BddProject, row.cnt) for row in rows]
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("list_projects query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("list_projects failed")
        raise


async def get_project(db: AsyncSession, project_id: uuid.UUID) -> BddProject | None:
    try:
        _query_result_for_logging = await db.get(BddProject, project_id)
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("get_project query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("get_project failed")
        raise


async def create_project(db: AsyncSession, name: str, description: str | None) -> BddProject:
    try:
        project = BddProject(name=name, description=description)
        db.add(project)
        await db.commit()
        await db.refresh(project)
        return project
    except Exception:
        logger.exception("create_project failed")
        raise


async def update_project(db: AsyncSession, project: BddProject, fields: dict) -> BddProject:
    try:
        for k, v in fields.items():
            setattr(project, k, v)
        await db.commit()
        await db.refresh(project)
        return project
    except Exception:
        logger.exception("update_project failed")
        raise


async def delete_project(db: AsyncSession, project: BddProject) -> None:
    try:
        await db.delete(project)
        await db.commit()
    except Exception:
        logger.exception("delete_project failed")
        raise


# ── Scenarios ─────────────────────────────────────────────────────────────────

async def list_scenarios(
    db: AsyncSession,
    project_id: uuid.UUID | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[BddScenario], int]:
    try:
        q = select(BddScenario).order_by(BddScenario.created_at.desc())
        if project_id is not None:
            q = q.where(BddScenario.project_id == project_id)
        if status is not None:
            q = q.where(BddScenario.status == status)

        total_q = select(func.count()).select_from(q.subquery())
        total = (await db.scalar(total_q)) or 0
        if total is None or not total:
            logger.info("list_scenarios query succeeded with 0 results")

        q = q.offset((page - 1) * page_size).limit(page_size)
        scenarios = list(await db.scalars(q))
        if scenarios is None or not scenarios:
            logger.info("list_scenarios query succeeded with 0 results")
        return scenarios, total
    except Exception:
        logger.exception("list_scenarios failed")
        raise


async def get_scenario(db: AsyncSession, scenario_id: uuid.UUID) -> BddScenario | None:
    try:
        _query_result_for_logging = await db.get(BddScenario, scenario_id)
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("get_scenario query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("get_scenario failed")
        raise


async def create_scenario(db: AsyncSession, **kwargs) -> BddScenario:
    try:
        tags = kwargs.pop("tags", None) or []
        scenario = BddScenario(**kwargs, tags=tags)
        db.add(scenario)
        await db.commit()
        await db.refresh(scenario)
        return scenario
    except Exception:
        logger.exception("create_scenario failed")
        raise


async def update_scenario(db: AsyncSession, scenario: BddScenario, fields: dict) -> BddScenario:
    try:
        for k, v in fields.items():
            setattr(scenario, k, v)
        await db.commit()
        await db.refresh(scenario)
        return scenario
    except Exception:
        logger.exception("update_scenario failed")
        raise


async def delete_scenario(db: AsyncSession, scenario: BddScenario) -> None:
    try:
        await db.delete(scenario)
        await db.commit()
    except Exception:
        logger.exception("delete_scenario failed")
        raise


# ── Settings ──────────────────────────────────────────────────────────────────

async def get_settings(db: AsyncSession) -> BddSettings:
    try:
        s = await db.get(BddSettings, 1)
        if s is None or not s:
            logger.info("get_settings query succeeded with 0 results")
        if s is None:
            s = BddSettings(id=1)
            db.add(s)
            await db.commit()
            await db.refresh(s)
        return s
    except Exception:
        logger.exception("get_settings failed")
        raise


async def update_settings(db: AsyncSession, fields: dict) -> BddSettings:
    try:
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
    except Exception:
        logger.exception("update_settings failed")
        raise


def settings_to_out(s: BddSettings) -> dict:
    """Returns dict for SettingsOut — masks encrypted fields."""
    try:
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
    except Exception:
        logger.exception("settings_to_out failed")
        raise


def get_decrypted_settings(s: BddSettings) -> dict:
    """Returns settings with decrypted credentials for use in AI/parser services."""
    try:
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
    except Exception:
        logger.exception("get_decrypted_settings failed")
        raise

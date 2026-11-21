# ruff: noqa: S101
"""Campi Jira della fase 3 sui modelli."""
from __future__ import annotations

import uuid

from sqlalchemy import delete

from app.models.test_lifecycle import TlFeature, TlFeatureStatus, TlIssueKind, TlScope
from tests._db import TestSession


async def test_jira_defaults_and_issue_kind():
    async with TestSession() as s:
        scope = TlScope(code="J" + uuid.uuid4().hex[:8], name="Jira")
        s.add(scope)
        await s.flush()
        feature = TlFeature(
            scope_id=scope.id, number=1, code=f"FEAT_001_{scope.code}",
            status=TlFeatureStatus.DRAFT, created_by="t@example.test",
        )
        s.add(feature)
        await s.commit()
        await s.refresh(feature)
        await s.refresh(scope)
        try:
            assert scope.jira_qa_epic_key is None and scope.jira_component is None
            assert feature.jira_qa_epic_key is None
            assert feature.jira_cache == {}
            assert feature.jira_error is None
            assert TlIssueKind("jira_creation_failed") is TlIssueKind.JIRA_CREATION_FAILED
        finally:
            await s.execute(delete(TlScope).where(TlScope.id == scope.id))
            await s.commit()

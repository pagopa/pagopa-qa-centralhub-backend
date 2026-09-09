from __future__ import annotations

from app.models.audit_log import AuditLog
from app.models.bdd import BddProject, BddScenario, BddSettings  # noqa: F401
from app.models.docs import DocItem  # noqa: F401
from app.models.bug import Bug
from app.models.gpd_position import GpdPositionSnapshot, GpdPositionSyncStatus  # noqa: F401
from app.models.coverage import Coverage
from app.models.dashboard import Dashboard, DashboardWidget
from app.models.e2e import E2eRun, E2eSuite
from app.models.integration import Integration
from app.models.notification_rule import NotificationRule
from app.models.psp_fee import PspFeeService, PspFeeSyncStatus  # noqa: F401
from app.models.release import Release
from app.models.role import Role  # noqa: F401
from app.models.run import Run, RunStep
from app.models.suite import Suite
from app.models.user import User
from .test_metrics import TestSuite, TestRun, TestExecution

from app.models.tm import ExternalResource, ResourceAbsence  # noqa: F401

__all__ = [
    "User",
    "Integration",
    "Suite",
    "Run",
    "RunStep",
    "Coverage",
    "Bug",
    "Release",
    "Dashboard",
    "DashboardWidget",
    "NotificationRule",
    "AuditLog",
    "E2eSuite",
    "E2eRun",
    "BddProject",
    "BddScenario",
    "BddSettings",
    "DocItem",
    "PspFeeService",
    "PspFeeSyncStatus",
    "GpdPositionSnapshot",
    "GpdPositionSyncStatus",
    "Role",
    "ExternalResource",
    "ResourceAbsence",
    "TestSuite",
    "TestRun",
    "TestExecution",
]

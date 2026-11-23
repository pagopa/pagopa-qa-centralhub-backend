from __future__ import annotations

from app.models.audit_log import AuditLog
from app.models.bdd import BddProject, BddScenario, BddSettings  # noqa: F401
from app.models.bug import Bug
from app.models.coverage import Coverage
from app.models.dashboard import Dashboard, DashboardWidget
from app.models.docs import DocItem  # noqa: F401
from app.models.ea import (  # noqa: F401
    EaChange,
    EaDocLink,
    EaEdge,
    EaInfraLink,
    EaInfraResource,
    EaMeta,
    EaNode,
    EaQaSignal,
    EaRepoSnapshot,
    EaSyncRun,
    EaSyncStatus,
)
from app.models.ea_assistant import EaAssistantSettings  # noqa: F401
from app.models.feature_visibility import FeatureVisibility  # noqa: F401
from app.models.flaky import FlakyRecomputeStatus, FlakyRegistry, FlakyScore  # noqa: F401
from app.models.gpd_position import GpdPositionSnapshot, GpdPositionSyncStatus  # noqa: F401
from app.models.integration import Integration
from app.models.messaging import MsgDedupe, MsgDelivery, MsgEvent, MsgInbox  # noqa: F401
from app.models.psp_fee import PspFeeService, PspFeeSyncStatus  # noqa: F401
from app.models.release import Release
from app.models.role import Role  # noqa: F401
from app.models.run import Run, RunStep
from app.models.sanp_health import (
    SanpEnvironment,
    SanpHealthChange,
    SanpHealthResult,
    SanpHealthRun,
    SanpHealthSyncStatus,
    SanpImportStatus,
    SanpResultStatus,
    SanpSeverity,
)
from app.models.status_page import (  # noqa: F401
    SpApplication,
    SpDeployEvent,
    SpProduct,
    SpSyncStatus,
    SpVersionState,
)
from app.models.suite import Suite
from app.models.test_lifecycle import (  # noqa: F401
    TlFeature,
    TlFeatureRevision,
    TlFeatureVersion,
    TlGitSyncState,
    TlIssue,
    TlReviewComment,
    TlScenario,
    TlScenarioContent,
    TlScope,
    TlStepPhrase,
)
from app.models.tm import ExternalResource, ResourceAbsence  # noqa: F401
from app.models.user import User

from .test_metrics import TestExecution, TestRun, TestSuite

__all__ = [
    "User",
    "Integration",
    "Suite",
    "Run",
    "RunStep",
    "SanpEnvironment",
    "SanpImportStatus",
    "SanpResultStatus",
    "SanpSeverity",
    "SanpHealthRun",
    "SanpHealthResult",
    "SanpHealthChange",
    "SanpHealthSyncStatus",
    "Coverage",
    "Bug",
    "Release",
    "Dashboard",
    "DashboardWidget",
    "MsgEvent",
    "MsgDelivery",
    "MsgInbox",
    "MsgDedupe",
    "AuditLog",
    "FeatureVisibility",
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
    "TlScope",
    "TlFeature",
    "TlFeatureRevision",
    "TlScenario",
    "TlScenarioContent",
    "TlFeatureVersion",
    "TlReviewComment",
    "TlIssue",
    "TlGitSyncState",
    "TlStepPhrase",
]

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

SCHEMA = "qachub"


class TlFeatureStatus(str, enum.Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    DESIGNED = "designed"
    IMPLEMENTED = "implemented"
    RUNNING = "running"


class TlRevisionStatus(str, enum.Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    MERGED = "merged"
    SUPERSEDED = "superseded"


class TlScenarioKind(str, enum.Enum):
    SCENARIO = "scenario"
    OUTLINE = "outline"


class TlPolarity(str, enum.Enum):
    POSITIVO = "positivo"
    NEGATIVO = "negativo"


class TlIssueKind(str, enum.Enum):
    STRUCTURAL_CHANGE = "structural_change"
    NEW_SCENARIO_FROM_GIT = "new_scenario_from_git"
    FILE_DELETED = "file_deleted"
    UNKNOWN_FEATURE_CODE = "unknown_feature_code"
    UNMANAGED_FILE = "unmanaged_file"
    MISSING_HEADER = "missing_header"
    PARSE_ERROR = "parse_error"
    PR_CLOSED_UNMERGED = "pr_closed_unmerged"
    PR_CREATION_FAILED = "pr_creation_failed"
    JIRA_CREATION_FAILED = "jira_creation_failed"


class TlIssueStatus(str, enum.Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


def _enum(cls: type[enum.Enum], name: str) -> ENUM:
    return ENUM(
        cls,
        name=name,
        schema=SCHEMA,
        create_type=False,
        values_callable=lambda e: [m.value for m in e],
    )


tl_feature_status_enum = _enum(TlFeatureStatus, "tl_feature_status")
tl_revision_status_enum = _enum(TlRevisionStatus, "tl_revision_status")
tl_scenario_kind_enum = _enum(TlScenarioKind, "tl_scenario_kind")
tl_polarity_enum = _enum(TlPolarity, "tl_polarity")
tl_issue_kind_enum = _enum(TlIssueKind, "tl_issue_kind")
tl_issue_status_enum = _enum(TlIssueStatus, "tl_issue_status")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TlScope(Base):
    __tablename__ = "tl_scopes"
    __table_args__ = (
        Index("uq_tl_scopes_code_lower", text("lower(code)"), unique=True),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    area: Mapped[str | None] = mapped_column(String(255))
    repo_folder: Mapped[str | None] = mapped_column(String(255))
    owner_team: Mapped[str | None] = mapped_column(String(255))
    jira_qa_epic_key: Mapped[str | None] = mapped_column(String(32))
    jira_component: Mapped[str | None] = mapped_column(String(100))
    next_feature_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class TlFeature(Base):
    __tablename__ = "tl_features"
    __table_args__ = (
        UniqueConstraint("scope_id", "number", name="uq_tl_features_scope_number"),
        Index("ix_tl_features_scope_status", "scope_id", "status"),
        CheckConstraint(
            "test_type IN ('integration', 'e2e', 'api')", name="ck_tl_features_test_type"
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    scope_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.tl_scopes.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[TlFeatureStatus] = mapped_column(
        tl_feature_status_enum, nullable=False, default=TlFeatureStatus.DRAFT
    )
    design_owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    impl_owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    # file-level test type tag: integration | e2e | api (see services/test_lifecycle/tags.py)
    test_type: Mapped[str | None] = mapped_column(String(16))
    jira_design_key: Mapped[str | None] = mapped_column(String(32))
    jira_impl_key: Mapped[str | None] = mapped_column(String(32))
    jira_qa_epic_key: Mapped[str | None] = mapped_column(String(32))
    # stato dei task Jira collegati: {"<chiave>": {summary, status, category, ...}}
    jira_cache: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    jira_error: Mapped[str | None] = mapped_column(Text)
    running_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    migration_note: Mapped[str | None] = mapped_column(Text)
    migrated_from: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    import_source: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    alignment_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    working_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            f"{SCHEMA}.tl_feature_revisions.id",
            use_alter=True,
            name="fk_tl_features_working_revision",
        )
    )
    published_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            f"{SCHEMA}.tl_feature_revisions.id",
            use_alter=True,
            name="fk_tl_features_published_revision",
        )
    )
    next_scenario_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    git_path: Mapped[str | None] = mapped_column(String(500))
    git_blob_sha: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class TlFeatureRevision(Base):
    __tablename__ = "tl_feature_revisions"
    __table_args__ = (
        UniqueConstraint("feature_id", "number", name="uq_tl_revisions_feature_number"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    feature_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.tl_features.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[TlRevisionStatus] = mapped_column(
        tl_revision_status_enum, nullable=False, default=TlRevisionStatus.DRAFT
    )
    base_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.tl_feature_revisions.id")
    )
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    background_steps: Mapped[str | None] = mapped_column(Text)
    requirements_url: Mapped[str | None] = mapped_column(String(1024))
    product_issue_key: Mapped[str | None] = mapped_column(String(32))
    # Jira keys used by this revision; the feature columns mirror the current revision's keys.
    jira_design_key: Mapped[str | None] = mapped_column(String(32))
    jira_impl_key: Mapped[str | None] = mapped_column(String(32))
    jira_qa_epic_key: Mapped[str | None] = mapped_column(String(32))
    reverse_engineering: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    design_estimate_days: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    impl_estimate_days: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    origin: Mapped[str] = mapped_column(
        String(10), nullable=False, default="hub", server_default="hub"
    )  # "hub" | "git"
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(String(1024))
    pr_branch: Mapped[str | None] = mapped_column(String(255))
    pr_state: Mapped[str | None] = mapped_column(String(10))  # open | merged | closed
    pr_error: Mapped[str | None] = mapped_column(Text)
    base_commit_sha: Mapped[str | None] = mapped_column(String(64))
    # working branch in the central repository and the two blob shas of the feature file as of
    # the last sync (see services/test_lifecycle/branch_sync_rules.py)
    work_branch: Mapped[str | None] = mapped_column(String(255))
    work_file_sha: Mapped[str | None] = mapped_column(String(64))  # repository side of the sync
    work_hub_sha: Mapped[str | None] = mapped_column(String(64))  # hub side: blob of the render
    merged_commit_sha: Mapped[str | None] = mapped_column(String(64))
    base_stale: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class TlScenario(Base):
    """Stable identity of a scenario within a feature (survives across revisions)."""

    __tablename__ = "tl_scenarios"
    __table_args__ = (
        UniqueConstraint("feature_id", "number", name="uq_tl_scenarios_feature_number"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    feature_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.tl_features.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    tag: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class TlScenarioAlias(Base):
    """Tag storico (senza @) che identifica ancora uno scenario nelle esecuzioni NRT."""

    __tablename__ = "tl_scenario_aliases"
    __table_args__ = (
        Index("ix_tl_scenario_aliases_scenario", "scenario_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    scenario_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.tl_scenarios.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at: Mapped[datetime] = _created_at()


class TlScenarioContent(Base):
    __tablename__ = "tl_scenario_contents"
    __table_args__ = (
        UniqueConstraint("revision_id", "scenario_id", name="uq_tl_contents_revision_scenario"),
        Index("ix_tl_scenario_contents_revision_position", "revision_id", "position"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.tl_feature_revisions.id", ondelete="CASCADE"), nullable=False
    )
    scenario_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.tl_scenarios.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    kind: Mapped[TlScenarioKind] = mapped_column(
        tl_scenario_kind_enum, nullable=False, default=TlScenarioKind.SCENARIO
    )
    polarity: Mapped[TlPolarity] = mapped_column(
        tl_polarity_enum, nullable=False, default=TlPolarity.POSITIVO
    )
    extra_tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    steps: Mapped[str] = mapped_column(Text, nullable=False)
    examples: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    detail_link: Mapped[str | None] = mapped_column(String(1024))


class TlFeatureVersion(Base):
    __tablename__ = "tl_feature_versions"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = _uuid_pk()
    feature_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.tl_features.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.tl_feature_revisions.id", ondelete="CASCADE"), nullable=False
    )
    origin: Mapped[str] = mapped_column(String(10), nullable=False)  # "hub" | "git"
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class TlReviewComment(Base):
    __tablename__ = "tl_review_comments"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = _uuid_pk()
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.tl_feature_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.tl_scenarios.id", ondelete="SET NULL")
    )
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    resolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = _created_at()


class TlIssue(Base):
    __tablename__ = "tl_issues"
    __table_args__ = (
        Index("ix_tl_issues_status_created_at", "status", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    feature_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.tl_features.id", ondelete="CASCADE")
    )
    revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.tl_feature_revisions.id", ondelete="SET NULL")
    )
    kind: Mapped[TlIssueKind] = mapped_column(tl_issue_kind_enum, nullable=False)
    status: Mapped[TlIssueStatus] = mapped_column(
        tl_issue_status_enum, nullable=False, default=TlIssueStatus.OPEN
    )
    path: Mapped[str | None] = mapped_column(String(500))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_by: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = _updated_at()


class TlGitSyncState(Base):
    """Single row (id=1) holding the state of the last git sync run."""

    __tablename__ = "tl_git_sync_state"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_main_sha: Mapped[str | None] = mapped_column(String(64))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class TlStepPhrase(Base):
    __tablename__ = "tl_step_phrases"
    __table_args__ = (
        UniqueConstraint("keyword", "text", name="uq_tl_step_phrases_keyword_text"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    keyword: Mapped[str] = mapped_column(String(10), nullable=False)  # given | when | then
    text: Mapped[str] = mapped_column(Text, nullable=False)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    implemented: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    updated_at: Mapped[datetime] = _updated_at()


# ── Migrazione da Confluence (fase 5) ─────────────────────────────────────────


class TlMigrationItem(Base):
    """Una feature da migrare: proposta dall'analisi, decisa da una persona, poi eseguita."""

    __tablename__ = "tl_migration_items"
    __table_args__ = (
        Index(
            "uq_tl_migration_items_page", "page_id", unique=True,
            postgresql_where=text("page_id IS NOT NULL"),
        ),
        Index(
            "uq_tl_migration_items_file", "file_path", unique=True,
            postgresql_where=text("file_path IS NOT NULL"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    page_id: Mapped[str | None] = mapped_column(String(32))
    page_version: Mapped[int | None] = mapped_column(Integer)
    page_title: Mapped[str | None] = mapped_column(String(500))
    file_path: Mapped[str | None] = mapped_column(String(1024))
    file_blob_sha: Mapped[str | None] = mapped_column(String(64))
    scope_code: Mapped[str] = mapped_column(String(20), nullable=False)
    number: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[TlFeatureStatus] = mapped_column(tl_feature_status_enum, nullable=False)
    decision: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    problems: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    proposal: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    feature_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.tl_features.id", ondelete="SET NULL")
    )
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class TlMigrationScope(Base):
    __tablename__ = "tl_migration_scopes"
    __table_args__ = ({"schema": SCHEMA},)

    scope_code: Mapped[str] = mapped_column(String(20), primary_key=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(String(1024))
    pr_state: Mapped[str | None] = mapped_column(String(16))
    pr_error: Mapped[str | None] = mapped_column(Text)
    confluence_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    updated_at: Mapped[datetime] = _updated_at()


class TlMigrationTag(Base):
    __tablename__ = "tl_migration_tags"
    __table_args__ = ({"schema": SCHEMA},)

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decision: Mapped[str] = mapped_column(
        String(8), nullable=False, default="keep", server_default="keep"
    )


class TlCategoryTag(Base):
    """Tag di categoria ammessi sugli scenari (oltre a identità e polarità)."""

    __tablename__ = "tl_category_tags"
    __table_args__ = ({"schema": SCHEMA},)

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = _created_at()


class TlMigrationState(Base):
    __tablename__ = "tl_migration_state"
    __table_args__ = ({"schema": SCHEMA},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="idle")
    error: Mapped[str | None] = mapped_column(Text)

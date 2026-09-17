from __future__ import annotations

# ruff: noqa: S101
from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

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


def test_sanp_health_enum_values() -> None:
    assert [value.value for value in SanpEnvironment] == ["SANP", "COLLAUDO", "PRODUZIONE"]
    assert [value.value for value in SanpImportStatus] == ["complete", "partial", "failed"]
    assert [value.value for value in SanpResultStatus] == [
        "KO",
        "WARNING",
        "INFO",
        "OK",
        "NOT_CONFIGURED",
    ]
    assert [value.value for value in SanpSeverity] == ["error", "warning", "info"]


def test_sanp_health_table_names_and_database_types() -> None:
    assert SanpHealthRun.__tablename__ == "sanp_health_runs"
    assert SanpHealthResult.__tablename__ == "sanp_health_results"
    assert SanpHealthChange.__tablename__ == "sanp_health_changes"
    assert SanpHealthSyncStatus.__tablename__ == "sanp_health_sync_status"

    assert isinstance(SanpHealthRun.__table__.c.github_run_id.type, BigInteger)
    assert SanpHealthRun.__table__.c.github_run_id.unique is True
    assert isinstance(SanpHealthRun.__table__.c.import_status.type, Enum)
    assert SanpHealthRun.__table__.c.started_at.type.timezone is True
    assert SanpHealthRun.__table__.c.completed_at.type.timezone is True
    assert SanpHealthRun.__table__.c.synced_at.type.timezone is True
    assert isinstance(SanpHealthResult.__table__.c.raw_payload.type, JSONB)
    assert isinstance(SanpHealthSyncStatus.__table__.c.last_attempt_at.type, DateTime)
    assert SanpHealthSyncStatus.__table__.c.last_attempt_at.type.timezone is True
    assert SanpHealthSyncStatus.__table__.c.last_success_at.type.timezone is True


def test_result_uniqueness_and_cascades_are_configured() -> None:
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in SanpHealthResult.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("run_id", "spec_name", "environment") in unique_columns

    result_fk = next(iter(SanpHealthResult.__table__.c.run_id.foreign_keys))
    change_fk = next(iter(SanpHealthChange.__table__.c.result_id.foreign_keys))
    assert result_fk.ondelete == "CASCADE"
    assert change_fk.ondelete == "CASCADE"
    assert SanpHealthRun.results.property.cascade.delete_orphan
    assert SanpHealthResult.changes.property.cascade.delete_orphan
    assert SanpHealthResult.run.property.back_populates == "results"
    assert SanpHealthChange.result.property.back_populates == "changes"


def test_nullable_audit_and_error_fields() -> None:
    assert SanpHealthRun.__table__.c.error_message.nullable is True
    assert SanpHealthChange.__table__.c.path.nullable is True
    assert SanpHealthChange.__table__.c.operation.nullable is True
    assert SanpHealthChange.__table__.c.section.nullable is True
    assert SanpHealthChange.__table__.c.comment.nullable is True


def test_query_indexes_are_present_in_orm_metadata() -> None:
    run_indexes = {tuple(index.columns.keys()) for index in SanpHealthRun.__table__.indexes}
    result_indexes = {tuple(index.columns.keys()) for index in SanpHealthResult.__table__.indexes}

    assert ("completed_at",) in run_indexes
    assert ("environment", "status") in result_indexes


def test_sync_status_is_constrained_to_singleton_id() -> None:
    checks = {
        str(constraint.sqltext)
        for constraint in SanpHealthSyncStatus.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "id = 1" in checks
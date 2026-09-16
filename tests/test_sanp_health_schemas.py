from __future__ import annotations

# ruff: noqa: S101
import uuid
from datetime import UTC, datetime

from app.models.sanp_health import SanpEnvironment, SanpResultStatus
from app.schemas.sanp_health import (
    SanpHealthMatrixCellOut,
    SanpHealthMatrixRowOut,
    SanpHealthReportDetailOut,
    SanpHealthRunOut,
)


def test_report_detail_serializes_matrix_response() -> None:
    run_id = uuid.uuid4()
    completed_at = datetime(2026, 9, 16, 10, 30, tzinfo=UTC)
    response = SanpHealthReportDetailOut(
        report=SanpHealthRunOut(
            id=run_id,
            github_run_id=9876543210,
            run_number=42,
            head_sha="abc123",
            workflow_branch="main",
            conclusion="success",
            html_url="https://github.com/pagopa/pagopa-api/actions/runs/9876543210",
            started_at=datetime(2026, 9, 16, 10, 0, tzinfo=UTC),
            completed_at=completed_at,
            synced_at=datetime(2026, 9, 16, 10, 35, tzinfo=UTC),
            import_status="complete",
            error_message=None,
            error_count=0,
            warning_count=1,
            info_count=0,
        ),
        sanp_version="3.9.1",
        items=[
            SanpHealthMatrixRowOut(
                spec_name="payments.yaml",
                display_name="Payments API",
                description="Payment operations",
                sanp=SanpHealthMatrixCellOut(
                    id=uuid.uuid4(),
                    environment=SanpEnvironment.SANP,
                    status=SanpResultStatus.WARNING,
                    source_branch="SANP 3.9.1",
                    target_apim="DEV",
                    display_name="Payments API",
                    description="Payment operations",
                    error_count=0,
                    warning_count=1,
                    info_count=0,
                    changes=[],
                ),
                collaudo=SanpHealthMatrixCellOut.not_configured(
                    SanpEnvironment.COLLAUDO
                ),
                produzione=SanpHealthMatrixCellOut.not_configured(
                    SanpEnvironment.PRODUZIONE
                ),
            )
        ],
    )

    payload = response.model_dump(mode="json")

    assert payload["report"]["id"] == str(run_id)
    assert payload["report"]["completed_at"] == "2026-09-16T10:30:00Z"
    assert payload["items"][0]["sanp"]["status"] == "WARNING"
    assert payload["items"][0]["collaudo"] == {
        "id": None,
        "environment": "COLLAUDO",
        "status": "NOT_CONFIGURED",
        "source_branch": None,
        "target_apim": None,
        "display_name": None,
        "description": None,
        "error_count": 0,
        "warning_count": 0,
        "info_count": 0,
        "changes": [],
    }


def test_missing_environment_cell_accepts_nullable_details() -> None:
    cell = SanpHealthMatrixCellOut(
        id=None,
        environment=SanpEnvironment.PRODUZIONE,
        status=SanpResultStatus.NOT_CONFIGURED,
        source_branch=None,
        target_apim=None,
        display_name=None,
        description=None,
        error_count=0,
        warning_count=0,
        info_count=0,
        changes=[],
    )

    assert cell.id is None
    assert cell.source_branch is None
    assert cell.target_apim is None
    assert cell.display_name is None
    assert cell.description is None
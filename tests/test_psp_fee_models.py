from __future__ import annotations

from app.models.psp_fee import PspFeeService, PspFeeSyncStatus


def test_psp_fee_service_tablename() -> None:
    assert PspFeeService.__tablename__ == "psp_fee_services"


def test_psp_fee_sync_status_tablename() -> None:
    assert PspFeeSyncStatus.__tablename__ == "psp_fee_sync_status"

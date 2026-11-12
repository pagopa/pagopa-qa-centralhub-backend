from __future__ import annotations

from app.core.permissions import ACTION_CATALOG, ACTION_KEYS, compute_role_matrix
from app.models.role import Role


def test_action_keys_match_catalog() -> None:
    assert ACTION_KEYS == {entry["key"] for entry in ACTION_CATALOG}


def test_action_catalog_size() -> None:
    assert len(ACTION_CATALOG) == 26


def test_quality_gate_report_is_readable_by_every_role_including_guest() -> None:
    entry = next(e for e in ACTION_CATALOG if e["key"] == "view:qg_report")
    assert all(entry["defaults"].values())
    # The list stays hidden from guests: only the single-run report is open to them.
    qg_list = next(e for e in ACTION_CATALOG if e["key"] == "view:qg_nrts")
    assert qg_list["defaults"]["guest"] is False


def test_tas_and_nrt_reports_are_readable_by_every_role_including_guest() -> None:
    for report, listing in [("view:tas_report", "view:tas_nrts"), ("view:nrts_report", "view:nrts")]:
        entry = next(e for e in ACTION_CATALOG if e["key"] == report)
        assert all(entry["defaults"].values())
        list_entry = next(e for e in ACTION_CATALOG if e["key"] == listing)
        assert list_entry["defaults"]["guest"] is False


def test_action_catalog_has_data_quality_entries() -> None:
    assert "view:data_quality" in ACTION_KEYS
    assert "manage:data_quality" in ACTION_KEYS


def test_compute_role_matrix_uses_defaults_for_qa_engineer() -> None:
    role = Role(key="qa_engineer", label="QA Engineer", is_system=False, permissions={})
    matrix = compute_role_matrix([role])
    assert matrix["qa_engineer"]["view:bdd"] is True
    assert matrix["qa_engineer"]["manage:integrations"] is False


def test_compute_role_matrix_uses_defaults_for_guest() -> None:
    role = Role(key="guest", label="Guest", is_system=False, permissions={})
    matrix = compute_role_matrix([role])
    assert matrix["guest"]["view:overview"] is True
    assert matrix["guest"]["view:bdd"] is False


def test_compute_role_matrix_applies_overrides() -> None:
    role = Role(key="guest", label="Guest", is_system=False, permissions={"view:bdd": True})
    matrix = compute_role_matrix([role])
    assert matrix["guest"]["view:bdd"] is True
    assert matrix["guest"]["view:overview"] is True  # default, unchanged


def test_compute_role_matrix_superadmin_is_always_true() -> None:
    role = Role(key="superadmin", label="Superadmin", is_system=True, permissions={})
    matrix = compute_role_matrix([role])
    assert all(matrix["superadmin"].values())
    assert set(matrix["superadmin"].keys()) == ACTION_KEYS


def test_run_actions_mirror_run_qg_defaults() -> None:
    by_key = {e["key"]: e for e in ACTION_CATALOG}
    for key in ("run:tas", "run:nrt"):
        assert by_key[key]["defaults"] == by_key["run:qg"]["defaults"]

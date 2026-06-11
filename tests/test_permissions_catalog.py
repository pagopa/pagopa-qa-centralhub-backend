from __future__ import annotations

from app.core.permissions import ACTION_CATALOG, ACTION_KEYS, compute_role_matrix
from app.models.role import Role


def test_action_keys_match_catalog() -> None:
    assert ACTION_KEYS == {entry["key"] for entry in ACTION_CATALOG}


def test_action_catalog_has_ten_entries() -> None:
    assert len(ACTION_CATALOG) == 10


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

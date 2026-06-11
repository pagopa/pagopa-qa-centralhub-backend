from __future__ import annotations

from app.models.role import Role
from app.models.user import User


def test_role_tablename() -> None:
    assert Role.__tablename__ == "roles"


def test_role_is_system_default_false() -> None:
    assert Role.__table__.c.is_system.default.arg is False


def test_role_permissions_default_empty_dict() -> None:
    assert Role.__table__.c.permissions.default.arg == {}


def test_user_role_default_guest() -> None:
    assert User.__table__.c.role.default.arg == "guest"


def test_user_role_has_foreign_key_to_roles() -> None:
    fks = {fk.target_fullname for fk in User.__table__.c.role.foreign_keys}
    assert "roles.key" in fks

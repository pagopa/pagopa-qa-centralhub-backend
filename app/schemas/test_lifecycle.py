from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

SCOPE_CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,19}$")


def _check_scope_code(value: str) -> str:
    if not SCOPE_CODE_PATTERN.fullmatch(value):
        raise ValueError(
            "Il codice deve iniziare con una lettera e contenere solo lettere e cifre "
            "(2-20 caratteri)"
        )
    return value


# ── Ambiti ────────────────────────────────────────────────────────────────────

class TlScopeCreate(BaseModel):
    code: str
    name: str
    area: str | None = None
    repo_folder: str | None = None
    owner_team: str | None = None

    _validate_code = field_validator("code")(_check_scope_code)


class TlScopeUpdate(BaseModel):
    code: str | None = None
    name: str | None = None
    area: str | None = None
    repo_folder: str | None = None
    owner_team: str | None = None

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str | None) -> str | None:
        return None if value is None else _check_scope_code(value)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("Il nome non può essere nullo")
        return value


class TlScopeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    area: str | None = None
    repo_folder: str | None = None
    owner_team: str | None = None
    next_feature_number: int
    created_at: datetime
    updated_at: datetime
    feature_count: int = 0
    status_counts: dict[str, int] = {}

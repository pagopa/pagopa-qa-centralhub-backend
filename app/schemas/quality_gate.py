from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SUITE_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
TAGS_PATTERN = r"^[@A-Za-z0-9_,.~-]{0,200}$"


class QualityGateRunCreate(BaseModel):
    test_suite: str = Field(pattern=SUITE_PATTERN)
    test_type: Literal["integration", "e2e", "api"] = "integration"
    environment: Literal["dev", "uat"] = "uat"
    tags: str = Field(default="@runnable", pattern=TAGS_PATTERN)
    allure: bool = True


class QualityGateRunOut(BaseModel):
    correlation_id: str
    run_name: str
    actions_url: str


CORRELATION_ID_PATTERN = r"^qg-[0-9a-f]{32}$"


class QualityGateRunStatus(BaseModel):
    """Live state of a launched run, read from GitHub Actions.

    ``pending`` means GitHub has not listed the run yet (dispatch is asynchronous).
    ``conclusion`` is only set once ``state`` is ``completed``.
    """

    correlation_id: str
    run_name: str
    state: Literal["pending", "queued", "in_progress", "completed"]
    conclusion: str | None = None
    html_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

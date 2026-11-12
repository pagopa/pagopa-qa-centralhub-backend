from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SUITE_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
TAGS_PATTERN = r"^[@A-Za-z0-9_,.~-]{0,200}$"


class QualityGateRunCreate(BaseModel):
    test_suite: str = Field(pattern=SUITE_PATTERN)
    test_type: Literal["integration", "e2e", "api"] = "integration"
    environment: Literal["dev", "uat"] = "uat"
    tags: str = Field(default="@runnable", pattern=TAGS_PATTERN)
    allure: bool = True


class NrtRunCreate(BaseModel):
    """Launch of the NRT workflow: every suite configured for the environment, or a selection."""

    suites: Literal["all"] | list[Annotated[str, Field(pattern=SUITE_PATTERN)]] = "all"
    target_env: Literal["dev", "uat"] = "uat"

    @field_validator("suites")
    @classmethod
    def _not_empty(cls, value: object) -> object:
        if isinstance(value, list) and not value:
            raise ValueError("select at least one suite or use 'all'")
        return value


class NrtSuiteOut(BaseModel):
    id: str
    label: str
    envs: list[str]


class NrtLaunchOptionsOut(BaseModel):
    """Suites the NRT workflow can run, with the environments each one supports.

    `available` is false when the registry could not be read: the client must then not
    restrict the choice, because the launch does not validate in that case either.
    """

    available: bool
    suites: list[NrtSuiteOut]


class QualityGateRunOut(BaseModel):
    correlation_id: str
    run_name: str
    actions_url: str


class QualityGateSuitesOut(BaseModel):
    """Suites that can be launched and the test types each one has in the TAS repository.

    `available` is false when the repository could not be read: the client must then not
    restrict the choice, because the launch itself does not validate in that case either.
    """

    available: bool
    suites: dict[str, list[str]]


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


class QualityGateScenarioOut(BaseModel):
    """A scenario of a Quality Gate run, without the (large) raw Allure payload."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scenario_name: str | None = None
    status: str | None = None
    duration_ms: int | None = None
    retries: int | None = None
    error_message: str | None = None
    allure_url: str | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _status_value(cls, value: object) -> object:
        # The ORM exposes an Enum member; the API returns its plain value (e.g. "FAILED").
        return getattr(value, "value", value)

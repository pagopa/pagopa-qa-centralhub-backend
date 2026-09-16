from __future__ import annotations

import io
import json
import zipfile
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field

MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
MAX_RESULT_JSON_BYTES = 2 * 1024 * 1024

ENVIRONMENT_MAP = MappingProxyType({
    "DEV": "SANP",
    "UAT": "COLLAUDO",
    "PROD": "PRODUZIONE",
})

LEVEL_MAP = MappingProxyType({
    3: "error",
    2: "warning",
    1: "info",
})


class DriftArtifactError(Exception):
    pass


class DriftArtifactChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    level: Literal[1, 2, 3]
    id: Annotated[str, Field(max_length=255)]
    text: str
    path: str | None = None
    operation: Annotated[str, Field(max_length=20)] | None = None
    section: Annotated[str, Field(max_length=255)] | None = None
    comment: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def severity(self) -> Literal["error", "warning", "info"]:
        return LEVEL_MAP[self.level]  # type: ignore[return-value]


class DriftArtifactPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    file: Annotated[str, Field(max_length=500)]
    env: str
    branch: Annotated[str, Field(max_length=255)]
    display_name: Annotated[str, Field(max_length=500)]
    description: str
    changes: list[DriftArtifactChange]


def parse_drift_artifact(name: str, payload: bytes) -> DriftArtifactPayload | None:
    if name.lower().startswith("drift-main-"):
        return None
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise DriftArtifactError("artifact exceeds maximum size")

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            files = [member for member in archive.infolist() if not member.is_dir()]
            if len(files) != 1:
                raise DriftArtifactError("artifact must contain exactly one non-directory file")

            result_member = files[0]
            if result_member.filename != "result.json":
                raise DriftArtifactError("artifact must contain result.json at archive root")
            if result_member.file_size > MAX_RESULT_JSON_BYTES:
                raise DriftArtifactError("result.json exceeds maximum size")

            with archive.open(result_member) as result_file:
                result_bytes = result_file.read(MAX_RESULT_JSON_BYTES + 1)
    except DriftArtifactError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as exc:
        raise DriftArtifactError("invalid ZIP artifact") from exc

    if len(result_bytes) > MAX_RESULT_JSON_BYTES:
        raise DriftArtifactError("result.json exceeds maximum size")

    try:
        raw_result = json.loads(result_bytes)
        result = DriftArtifactPayload.model_validate(raw_result)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise DriftArtifactError("invalid result.json JSON payload") from exc

    if result.env == "MAIN":
        return None

    environment = ENVIRONMENT_MAP.get(result.env)
    if environment is None:
        raise DriftArtifactError(f"unknown environment: {result.env}")
    return result.model_copy(update={"env": environment})


def classify_result(changes: list[DriftArtifactChange]) -> str:
    levels = {change.level for change in changes}
    if 3 in levels:
        return "KO"
    if 2 in levels:
        return "WARNING"
    if 1 in levels:
        return "INFO"
    return "OK"
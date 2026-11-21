from __future__ import annotations

import enum
import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings
from app.models.test_lifecycle import (
    TlFeatureStatus,
    TlIssueKind,
    TlIssueStatus,
    TlPolarity,
    TlRevisionStatus,
    TlScenarioKind,
)
from app.services.test_lifecycle.gherkin import ALLOWED_EXTRA_TAGS

SCOPE_CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,19}$")


def _check_scope_code(value: str) -> str:
    if not SCOPE_CODE_PATTERN.fullmatch(value):
        raise ValueError(
            "Il codice deve iniziare con una lettera e contenere solo lettere e cifre "
            "(2-20 caratteri)"
        )
    return value


# ── Ambiti ────────────────────────────────────────────────────────────────────

def _check_epic_key(value: str | None) -> str | None:
    """Epic QA: chiave del progetto Jira dei task QA (es. PQ-495)."""
    if value is None:
        return None
    value = value.strip()
    project = re.escape(settings.tl_jira_project)
    if not re.fullmatch(rf"{project}-\d+", value):
        raise ValueError(f"Epic QA non valida: atteso {settings.tl_jira_project}-<numero>")
    return value


def _check_component(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) > 100:
        raise ValueError("Il componente Jira non può superare 100 caratteri")
    return value or None


class TlScopeCreate(BaseModel):
    code: str
    name: str
    area: str | None = None
    repo_folder: str | None = None
    owner_team: str | None = None
    jira_qa_epic_key: str | None = None
    jira_component: str | None = None

    _validate_code = field_validator("code")(_check_scope_code)
    _validate_epic = field_validator("jira_qa_epic_key")(_check_epic_key)
    _validate_component = field_validator("jira_component")(_check_component)


class TlScopeUpdate(BaseModel):
    code: str | None = None
    name: str | None = None
    area: str | None = None
    repo_folder: str | None = None
    owner_team: str | None = None
    jira_qa_epic_key: str | None = None
    jira_component: str | None = None

    _validate_epic = field_validator("jira_qa_epic_key")(_check_epic_key)
    _validate_component = field_validator("jira_component")(_check_component)

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
    jira_qa_epic_key: str | None = None
    jira_component: str | None = None
    next_feature_number: int
    created_at: datetime
    updated_at: datetime
    feature_count: int = 0
    status_counts: dict[str, int] = {}


# ── Feature, revisioni, scenari ───────────────────────────────────────────────

PQ_KEY_PATTERN = re.compile(r"^PQ-\d+$")
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
FEATURE_TITLE_MAX = 255  # limite della colonna tl_feature_revisions.title
SCENARIO_TITLE_MAX = 500
COMMENT_MAX_LEN = 5000  # corpo dei commenti e nota di "richiedi modifiche"

_DESCRIPTION_FORBIDDEN_RE = re.compile(
    r"^(@|#|(Funzionalit[àa]|Contesto|Scenario|Schema dello scenario|Esempi|Esempio|Regola"
    r"|Abilit[àa]|Esigenza di Business)\s*:)",
    re.IGNORECASE,
)


def _check_title(value: str, max_len: int) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Il titolo non può essere vuoto")
    if "\n" in value or "\r" in value:
        raise ValueError("Il titolo deve stare su una sola riga")
    if len(value) > max_len:
        raise ValueError(f"Il titolo può avere al massimo {max_len} caratteri")
    return value


def _feature_title(value: str) -> str:
    return _check_title(value, FEATURE_TITLE_MAX)


def _scenario_title(value: str) -> str:
    return _check_title(value, SCENARIO_TITLE_MAX)


def _check_pattern(value: str | None, pattern: re.Pattern[str], label: str) -> str | None:
    if value is None:
        return None
    if not pattern.fullmatch(value):
        raise ValueError(f"Chiave Jira non valida per {label}")
    return value


def _check_description(value: str | None) -> str | None:
    if value is None:
        return None
    for line in value.splitlines():
        if _DESCRIPTION_FORBIDDEN_RE.match(line.strip()):
            raise ValueError(
                "La descrizione non può contenere righe che iniziano con @, # "
                "o con una parola chiave di sezione Gherkin"
            )
    return value


URL_MAX_LEN = 1024
_URL_SCHEME_RE = re.compile(r"^https?://\S", re.IGNORECASE)


def _check_url(value: str | None) -> str | None:
    """Link su una sola riga, http(s); vuoto → null (finisce nel feature file generato)."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if any(c in value for c in "\r\n\v\f\x85\u2028\u2029"):
        raise ValueError("Il link deve stare su una sola riga")
    if not _URL_SCHEME_RE.match(value):
        raise ValueError("Il link deve iniziare con http:// o https://")
    if len(value) > URL_MAX_LEN:
        raise ValueError(f"Il link può avere al massimo {URL_MAX_LEN} caratteri")
    return value


def _check_extra_tags(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    unknown = [t for t in value if t not in ALLOWED_EXTRA_TAGS]
    if unknown:
        raise ValueError(
            f"Tag non ammessi: {', '.join(unknown)}. Ammessi: {', '.join(ALLOWED_EXTRA_TAGS)}"
        )
    return list(dict.fromkeys(value))


class TlExamples(BaseModel):
    headers: list[str]
    rows: list[list[str]] = []

    @model_validator(mode="after")
    def _check(self) -> TlExamples:
        if not self.headers or any(not h.strip() for h in self.headers):
            raise ValueError("Gli Esempi richiedono intestazioni non vuote")
        if len(set(self.headers)) != len(self.headers):
            raise ValueError("Le intestazioni degli Esempi devono essere uniche")
        if any(len(r) != len(self.headers) for r in self.rows):
            raise ValueError("Ogni riga degli Esempi deve avere tante celle quante le intestazioni")
        return self


class TlFeatureCreate(BaseModel):
    scope_id: uuid.UUID
    title: str
    design_owner_id: uuid.UUID | None = None
    actor_email: str

    _v_title = field_validator("title")(_feature_title)


class TlFeatureUpdate(BaseModel):
    design_owner_id: uuid.UUID | None = None
    reviewer_id: uuid.UUID | None = None
    impl_owner_id: uuid.UUID | None = None
    jira_design_key: str | None = None
    jira_impl_key: str | None = None
    jira_qa_epic_key: str | None = None
    actor_email: str

    _validate_epic = field_validator("jira_qa_epic_key")(_check_epic_key)

    @field_validator("jira_design_key")
    @classmethod
    def _v_design(cls, value: str | None) -> str | None:
        return _check_pattern(value, PQ_KEY_PATTERN, "la progettazione (es. PQ-12)")

    @field_validator("jira_impl_key")
    @classmethod
    def _v_impl(cls, value: str | None) -> str | None:
        return _check_pattern(value, ISSUE_KEY_PATTERN, "l'implementazione (es. ABC-12)")


class TlRevisionUpdate(BaseModel):
    lock_version: int
    actor_email: str
    title: str | None = None
    description: str | None = None
    background_steps: str | None = None
    requirements_url: str | None = Field(default=None, max_length=1024)
    product_issue_key: str | None = None
    reverse_engineering: bool | None = None
    design_estimate_days: Decimal | None = Field(default=None, ge=0, max_digits=5, decimal_places=1)
    impl_estimate_days: Decimal | None = Field(default=None, ge=0, max_digits=5, decimal_places=1)

    @field_validator("title")
    @classmethod
    def _v_title(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("Il titolo non può essere nullo")
        return _feature_title(value)

    _v_description = field_validator("description")(_check_description)
    _v_requirements_url = field_validator("requirements_url")(_check_url)

    @field_validator("product_issue_key")
    @classmethod
    def _v_product(cls, value: str | None) -> str | None:
        return _check_pattern(value, ISSUE_KEY_PATTERN, "l'Epic/Story prodotto")

    @field_validator("reverse_engineering")
    @classmethod
    def _v_reverse(cls, value: bool | None) -> bool:
        if value is None:
            raise ValueError("Valore non nullo richiesto")
        return value


class TlScenarioCreate(BaseModel):
    actor_email: str
    title: str
    kind: TlScenarioKind = TlScenarioKind.SCENARIO
    polarity: TlPolarity = TlPolarity.POSITIVO

    _v_title = field_validator("title")(_scenario_title)


class TlScenarioUpdate(BaseModel):
    actor_email: str
    lock_version: int
    title: str | None = None
    kind: TlScenarioKind | None = None
    polarity: TlPolarity | None = None
    extra_tags: list[str] | None = None
    steps: str | None = None
    examples: TlExamples | None = None
    detail_link: str | None = Field(default=None, max_length=1024)
    position: int | None = Field(default=None, ge=1)

    @field_validator("title")
    @classmethod
    def _v_title(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("Il titolo non può essere nullo")
        return _scenario_title(value)

    _v_tags = field_validator("extra_tags")(_check_extra_tags)
    _v_detail_link = field_validator("detail_link")(_check_url)

    @field_validator("kind", "polarity", "steps", "position", "extra_tags")
    @classmethod
    def _v_not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Valore non nullo richiesto")
        return value


class TlLineError(BaseModel):
    line: int
    message: str


class TlScenarioOut(BaseModel):
    id: uuid.UUID
    number: int
    tag: str
    position: int
    title: str
    kind: TlScenarioKind
    polarity: TlPolarity
    extra_tags: list[str] = []
    steps: str
    examples: dict[str, Any] | None = None
    detail_link: str | None = None
    errors: list[TlLineError] = []


class TlRevisionOut(BaseModel):
    id: uuid.UUID
    number: int
    status: TlRevisionStatus
    lock_version: int
    title: str
    description: str | None = None
    background_steps: str | None = None
    requirements_url: str | None = None
    product_issue_key: str | None = None
    reverse_engineering: bool = False
    design_estimate_days: Decimal | None = None
    impl_estimate_days: Decimal | None = None
    created_by: str
    approved_by: str | None = None
    approved_at: datetime | None = None
    origin: str = "hub"
    pr_number: int | None = None
    pr_url: str | None = None
    pr_state: Literal["open", "merged", "closed"] | None = None
    pr_error: str | None = None
    base_stale: bool = False
    merged_commit_sha: str | None = None
    created_at: datetime
    updated_at: datetime
    scenarios: list[TlScenarioOut] = []
    background_errors: list[TlLineError] = []


class TlFeatureListItem(BaseModel):
    id: uuid.UUID
    code: str
    number: int
    title: str | None = None
    status: TlFeatureStatus
    scope_id: uuid.UUID
    scope_code: str
    design_owner_id: uuid.UUID | None = None
    reviewer_id: uuid.UUID | None = None
    impl_owner_id: uuid.UUID | None = None
    scenario_count: int = 0
    updated_at: datetime


class TlJiraLink(BaseModel):
    role: Literal["design", "impl", "qa_epic", "product"]
    key: str
    summary: str | None = None
    status: str | None = None
    category: Literal["new", "indeterminate", "done"] | None = None
    url: str | None = None
    verified: bool = False


class TlFeatureDetail(BaseModel):
    id: uuid.UUID
    code: str
    number: int
    status: TlFeatureStatus
    scope_id: uuid.UUID
    scope_code: str
    design_owner_id: uuid.UUID | None = None
    reviewer_id: uuid.UUID | None = None
    impl_owner_id: uuid.UUID | None = None
    jira_design_key: str | None = None
    jira_impl_key: str | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    working_revision: TlRevisionOut | None = None
    published_revision: TlRevisionOut | None = None
    transitions: list[TlTransitionOut] = []
    git_path: str | None = None
    open_issues: int = 0
    jira: list[TlJiraLink] = []
    jira_error: str | None = None
    jira_qa_epic_key: str | None = None
    jira_qa_epic_effective: str | None = None
    jira_warning: str | None = None


# ── Workflow e storico ────────────────────────────────────────────────────────

class TlTransitionAction(str, enum.Enum):
    SUBMIT = "submit"
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    REOPEN = "reopen"


class TlTransitionRequest(BaseModel):
    action: TlTransitionAction
    actor_email: str
    note: str | None = None

    @field_validator("note")
    @classmethod
    def _v_note(cls, value: str | None) -> str | None:
        # Stesso limite del corpo dei commenti: la nota diventa un commento di review.
        if value is not None and len(value.strip()) > COMMENT_MAX_LEN:
            raise ValueError(f"La nota non può superare {COMMENT_MAX_LEN} caratteri")
        return value


class TlPrRequest(BaseModel):
    actor_email: str


class TlGitSyncSummaryOut(BaseModel):
    prs_merged: int
    prs_closed: int
    files_imported: int
    issues_opened: int
    steps_indexed: bool
    head_sha: str
    full_resync: bool = False


class TlGitStatusOut(BaseModel):
    configured: bool
    last_main_sha: str | None = None
    last_run_at: datetime | None = None
    last_error: str | None = None
    last_summary: dict[str, Any] | None = None


class TlTransitionOut(BaseModel):
    action: TlTransitionAction
    allowed: bool
    reasons: list[str] = []


class TlHistoryItem(BaseModel):
    kind: Literal["event", "version"]
    at: datetime
    actor: str | None = None
    action: str | None = None
    detail: dict[str, Any] | None = None
    version_id: uuid.UUID | None = None


class TlVersionOut(BaseModel):
    id: uuid.UUID
    feature_id: uuid.UUID
    revision_id: uuid.UUID
    origin: str
    snapshot: dict[str, Any]
    commit_sha: str | None = None
    actor: str
    created_at: datetime

# ── Commenti di review, anteprima, validazione live ──────────────────────────


class TlCommentCreate(BaseModel):
    actor_email: str
    body: str
    scenario_id: uuid.UUID | None = None

    @field_validator("body")
    @classmethod
    def _body(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Il commento non può essere vuoto")
        if len(value) > COMMENT_MAX_LEN:
            raise ValueError(f"Il commento non può superare {COMMENT_MAX_LEN} caratteri")
        return value


class TlCommentResolve(BaseModel):
    actor_email: str
    resolved: bool


class TlCommentOut(BaseModel):
    id: uuid.UUID
    revision_id: uuid.UUID
    scenario_id: uuid.UUID | None = None
    scenario_tag: str | None = None
    author: str
    body: str
    resolved: bool
    created_at: datetime


class TlPreviewError(BaseModel):
    scenario_tag: str | None = None
    line: int
    message: str


class TlPreviewOut(BaseModel):
    file: str
    errors: list[TlPreviewError] = []


class TlGherkinValidateRequest(BaseModel):
    steps: str
    kind: TlScenarioKind = TlScenarioKind.SCENARIO
    examples: TlExamples | None = None
    background: bool = False


class TlGherkinValidateOut(BaseModel):
    errors: list[TlLineError] = []



# ── Anomalie ─────────────────────────────────────────────────────────────────


class TlIssueOut(BaseModel):
    id: uuid.UUID
    feature_id: uuid.UUID | None = None
    revision_id: uuid.UUID | None = None
    kind: TlIssueKind
    status: TlIssueStatus
    path: str | None = None
    commit_sha: str | None = None
    detail: dict[str, Any] = {}
    created_at: datetime
    updated_by: str | None = None
    updated_at: datetime
    feature_code: str | None = None
    feature_title: str | None = None


class TlIssueUpdate(BaseModel):
    status: Literal["acknowledged", "resolved"]
    actor_email: str


# ── Indice degli step ────────────────────────────────────────────────────────


class TlStepPhraseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    keyword: Literal["given", "when", "then"]
    text: str
    occurrences: int
    implemented: bool


# ── Riallineamento a tre vie (fase 2) ──────────────────────────────────────────

RebaseSide = Literal["git", "draft"]


class TlScenarioContentOut(BaseModel):
    position: int
    title: str
    kind: TlScenarioKind
    polarity: TlPolarity
    extra_tags: list[str] = []
    steps: str
    examples: dict[str, Any] | None = None
    detail_link: str | None = None


class TlRebaseScenario(BaseModel):
    scenario_id: uuid.UUID
    tag: str
    base: TlScenarioContentOut | None = None
    git: TlScenarioContentOut | None = None
    draft: TlScenarioContentOut | None = None
    conflict: bool = False


class TlRebaseField(BaseModel):
    field: Literal["title", "description", "background"]
    base: str | None = None
    git: str | None = None
    draft: str | None = None
    conflict: bool = False


class TlRebaseView(BaseModel):
    revision_id: uuid.UUID
    base_revision_id: uuid.UUID | None = None
    published_revision_id: uuid.UUID | None = None
    lock_version: int
    metadata: list[TlRebaseField] = []
    scenarios: list[TlRebaseScenario] = []


class TlRebaseApply(BaseModel):
    lock_version: int
    actor_email: str
    choices: dict[str, RebaseSide] = {}


# ── Jira (fase 3) ──────────────────────────────────────────────────────────────


class TlJiraActorRequest(BaseModel):
    actor_email: str


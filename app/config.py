from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://qachub:changeme@localhost:5432/qachub"

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"
    celery_queue: str = "qa-centralhub"

    # Security
    jwt_secret: str = "change-me-in-production"
    encryption_key: str = "change-me-32-bytes-fernet-key====="

    # OIDC
    oidc_issuer: str = ""
    oidc_audience: str = "qa-hub-backend"

    # CORS
    cors_origins: list[str] = ["http://localhost:3000"]

    # App
    app_version: str = "0.1.0"
    debug: bool = False

    # Observability
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "qa-hub-backend"

    # GitHub integration
    github_token: str = ""

    # Quality Gate launch (TAS workflow dispatch). The token needs Actions: Read and write on
    # the TAS repository and is kept separate from the read-only github_token.
    tas_github_token: str = ""
    tas_repo: str = "pagopa/pagopa-platform-integration-test"
    tas_workflow_file: str = "test-automation-service.yml"
    nrt_workflow_file: str = "nrt-main-dispatch-tests.yml"
    tas_ref: str = "main"
    # Comma-separated suites that can be launched; empty means any well-formed suite name.
    qg_launch_suites: str = ""

    # Flaky scenarios scoring (design: docs/superpowers/specs/2026-11-14-flaky-*-design.md)
    flaky_window_days: int = 30
    flaky_max_runs: int = 30
    flaky_half_life_days: float = 14.0
    flaky_min_runs: int = 5
    flaky_broken_streak: int = 5
    flaky_unmark_min_runs: int = 10
    # Comma-separated lower bounds of levels 2, 3, 4 and 5 (score 0-100)
    flaky_level_thresholds: str = "5,15,30,50"

    @property
    def flaky_level_bounds(self) -> tuple[int, int, int, int]:
        values = tuple(int(part) for part in self.flaky_level_thresholds.split(","))
        if len(values) != 4:
            raise ValueError("flaky_level_thresholds must contain exactly four values")
        return values  # type: ignore[return-value]

    # EA Map, infrastructure view: also store VNet/subnet address prefixes (off: IPs, CIDRs
    # and subscription ids are never ingested, see docs/ea-map/DESIGN-infra.md §4)
    ea_infra_include_network_details: bool = False

    # Bootstrap defaults; runtime choices are stored in ea_assistant_settings.
    ea_assistant_provider: str = "gemini"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    # comma-separated models tried, in order, when the main one stays overloaded
    gemini_fallback_models: str = ""

    # Jira integration
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""

    # PSP Fee catalog (Data Hub)
    psp_fee_json_url: str = (
        "https://pagopa-afm-p-st-fee.s3.eu-central-1.amazonaws.com/output_elenco_servizi.json"
    )

    superset_main_dashboard_id: str = "1"
    superset_url: str = "http://localhost:8088"
    superset_guest_username: str = "service"
    superset_guest_first_name: str = "service"
    superset_guest_last_name: str = "service"
    superset_username: str = "admin"
    superset_password: str = ""


settings = Settings()

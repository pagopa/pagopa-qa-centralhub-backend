from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://qa_hub:changeme@localhost:5432/qa_hub"

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"

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

    # Jira integration
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""

    # PSP Fee catalog (Data Hub)
    psp_fee_json_url: str = "https://pagopa-afm-p-st-fee.s3.eu-central-1.amazonaws.com/output_elenco_servizi.json"


settings = Settings()

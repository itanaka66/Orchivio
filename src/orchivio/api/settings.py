"""Externalised configuration (12-factor). All variables use the ``ORCHIVIO_`` prefix."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORCHIVIO_", env_file=".env", extra="ignore")

    env: Literal["dev", "prod"] = "dev"
    database_url: str = "sqlite:///./orchivio.db"
    # Preview (runtime) data lives here, never in the platform database (FR-035).
    preview_database_url: str = "sqlite:///./preview.db"
    artifact_dir: str = "./artifacts"
    max_body_bytes: int = 1_048_576
    max_import_bytes: int = 8_388_608  # model archives (zip); see model/canonical.py for the inner limits
    # "none" disables the AI endpoints. "anthropic" needs ORCHIVIO_ANTHROPIC_API_KEY and
    # sends prompts to a hosted API; projects must opt in (settings.ai_allow_external).
    llm_provider: Literal["none", "anthropic"] = "none"
    llm_model: str | None = None
    anthropic_api_key: SecretStr | None = None

    # Accounts and sessions
    allow_registration: bool = True
    session_ttl_hours: int = 12
    session_cookie_name: str = "orchivio_session"
    # Cookie `Secure` flag. Must stay on in prod; set false only for plain-HTTP local dev.
    cookie_secure: bool | None = None  # None => true in prod, false in dev
    # /metrics is disabled unless a token is set; scrapers send `Authorization: Bearer <token>`.
    metrics_token: SecretStr | None = None
    login_max_failures: int = 5
    login_window_seconds: int = 900
    log_json: bool = False
    mcp_rate_per_minute: int = 120  # tools/call requests per API token
    # Background jobs: run the worker inside the API process (small installs) or use `orchivio worker`.
    worker_enabled: bool = False
    worker_interval_seconds: float = 15.0
    worker_lease_seconds: int = 120
    worker_per_app_limit: int = 2
    # Deployment packages are signed with Ed25519: base64 of the raw 32-byte seed (`orchivio keygen`). Extra
    # trusted public keys (base64) may verify packages signed elsewhere, e.g. by another installation.
    package_signing_key: SecretStr | None = None
    package_trusted_public_keys: list[str] = []
    inbound_max_body_bytes: int = 256 * 1024
    inbound_rate_per_minute: int = 60  # per inbound webhook
    mcp_client_max_calls: int = 3  # tool calls the AI Builder may make per proposal
    # OpenTelemetry (docs/OPERATIONS.md §7). Needs the `otel` extra; off by default.
    otel_enabled: bool = False
    otel_service_name: str = "orchivio"
    otel_exporter: str = "otlp"  # otlp | console | none
    otel_endpoint: str | None = None  # OTLP/HTTP base URL; else OTEL_EXPORTER_OTLP_ENDPOINT
    otel_sample_ratio: float = 1.0
    otel_metric_interval_ms: int = 30_000
    app_log_retention_days: int = 30  # application log lines kept in the platform database
    otel_logs: bool = True  # also ship the platform's own log lines over OTLP when an endpoint is set
    otel_propagate_outbound: bool = False  # add traceparent to webhook and consumed-API requests
    otel_trust_incoming_context: bool = False  # honour traceparent from callers (only behind a trusted gateway)
    public_url: str | None = None  # externally visible base URL (OIDC redirect URIs); e.g. https://studio.example.com
    csv_max_bytes: int = 2 * 1024 * 1024
    csv_max_rows: int = 5000
    csv_max_export_rows: int = 10_000
    # Outbound webhooks (docs/INTEGRATIONS.md). The two `allow_*` switches are for local development only.
    webhook_allow_http: bool = False
    webhook_allow_private_networks: bool = False
    webhook_allowed_hosts: list[str] = []  # fnmatch patterns; empty = any public host
    webhook_timeout_seconds: float = 10.0
    webhook_max_attempts: int = 6
    webhook_backoff_seconds: int = 30
    webhook_pause_after_failures: int = 10

    @field_validator("anthropic_api_key", "llm_model", "metrics_token", "package_signing_key", "public_url", "otel_endpoint", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        return None if v is None or (isinstance(v, str) and not v.strip()) else v

    @property
    def secure_cookies(self) -> bool:
        return self.env == "prod" if self.cookie_secure is None else self.cookie_secure


@lru_cache
def get_settings() -> Settings:
    return Settings()

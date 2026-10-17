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
    # Background jobs: run the worker inside the API process (small installs) or use `orchivio worker`.
    worker_enabled: bool = False
    worker_interval_seconds: float = 15.0
    worker_lease_seconds: int = 120
    worker_per_app_limit: int = 2

    @field_validator("anthropic_api_key", "llm_model", "metrics_token", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        return None if v is None or (isinstance(v, str) and not v.strip()) else v

    @property
    def secure_cookies(self) -> bool:
        return self.env == "prod" if self.cookie_secure is None else self.cookie_secure


@lru_cache
def get_settings() -> Settings:
    return Settings()

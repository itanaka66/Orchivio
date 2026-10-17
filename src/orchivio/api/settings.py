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
    artifact_dir: str = "./artifacts"
    # Static bootstrap token for the stateless tool endpoints. Required when env=prod.
    api_token: SecretStr | None = None
    max_body_bytes: int = 1_048_576
    llm_provider: Literal["mock"] = "mock"

    @field_validator("api_token", mode="before")
    @classmethod
    def _blank_token_is_unset(cls, v: object) -> object:
        return None if v is None or (isinstance(v, str) and not v.strip()) else v


@lru_cache
def get_settings() -> Settings:
    return Settings()

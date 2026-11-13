"""Relay configuration, loaded from environment variables (or a .env file)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    relay_api_key: str = Field(min_length=16)
    public_url: str = "http://localhost:8000"

    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = Field(min_length=1)
    ntfy_token: str | None = None

    request_ttl_seconds: int = Field(default=300, gt=0)
    cleanup_interval_seconds: int = Field(default=30, gt=0)
    max_poll_timeout_seconds: int = Field(default=60, gt=0)

    rate_limit_requests: int = Field(default=20, gt=0)
    rate_limit_window_seconds: int = Field(default=60, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

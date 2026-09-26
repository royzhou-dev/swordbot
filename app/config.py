"""Typed application configuration loaded from environment variables / `.env`."""

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    app_base_url: str | None = None

    database_url: str = "postgresql+asyncpg://swordbot:swordbot@localhost:5432/swordbot"

    # Event worker (PLAN D1). The retry defaults give up after about 30 minutes:
    # 8 attempts with backoff 15s, 30s, 1m, ... capped at 30m.
    worker_enabled: bool = True
    worker_concurrency: int = Field(default=4, ge=1)
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    event_max_attempts: int = Field(default=8, ge=1)
    event_retry_base_seconds: float = Field(default=15, gt=0)
    event_retry_max_seconds: float = Field(default=1800, gt=0)
    # How long a claimed event may run before it is presumed crashed and retried.
    event_lease_seconds: float = Field(default=300, gt=0)

    # Integration credentials are optional here so the app can boot before each
    # integration exists; the milestone that uses a credential validates it.
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5"

    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None
    telegram_allowed_user_id: int | None = None

    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_redirect_uri: str | None = None
    gmail_refresh_token: SecretStr | None = None

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    return Settings()

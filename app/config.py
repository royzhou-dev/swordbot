"""Typed application configuration loaded from environment variables / `.env`."""

import re
from enum import StrEnum
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_WEBHOOK_SECRET = re.compile(r"[A-Za-z0-9_-]{1,256}")


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    app_base_url: str | None = None
    # IANA name, e.g. America/Los_Angeles. Resolves "tonight" and "yesterday" to dates.
    user_timezone: str = "UTC"

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
    # Per request. The SDK retries brief failures itself; the worker's backoff
    # covers longer outages. Keep timeout x (retries + 1) x 2 (repair) well under
    # the handler cutoff (90% of EVENT_LEASE_SECONDS).
    openai_timeout_seconds: float = Field(default=45, gt=0)
    openai_max_retries: int = Field(default=1, ge=0)

    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None
    telegram_allowed_user_id: int | None = None
    # Overridable for tests or a self-hosted Bot API server.
    telegram_api_base_url: str = "https://api.telegram.org"

    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_redirect_uri: str | None = None
    gmail_refresh_token: SecretStr | None = None
    # Your Gmail address, for the From header. The `gmail.send` scope can't read
    # it, and the From display name follows each case's signature name.
    gmail_sender_address: str | None = None

    @field_validator("telegram_webhook_secret")
    @classmethod
    def _webhook_secret_charset(cls, value: SecretStr | None) -> SecretStr | None:
        # Telegram's setWebhook only accepts 1-256 characters from this set.
        if value is not None and not _WEBHOOK_SECRET.fullmatch(value.get_secret_value()):
            raise ValueError("must be 1-256 characters from A-Z, a-z, 0-9, _ and -")
        return value

    @field_validator("user_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}; use an IANA name") from exc
        return value

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def user_zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.user_timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()

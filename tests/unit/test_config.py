"""Deployment-facing settings: the Postgres URL format and the production startup check."""

import pytest
from pydantic import SecretStr

from app.config import (
    ConfigurationError,
    Environment,
    Settings,
    check_production_config,
    normalize_database_url,
    production_config_errors,
)
from app.main import create_app


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # Railway's DATABASE_URL format.
        (
            "postgresql://postgres:pw@postgres.railway.internal:5432/railway",
            "postgresql+asyncpg://postgres:pw@postgres.railway.internal:5432/railway",
        ),
        ("postgres://u:pw@db:5432/app", "postgresql+asyncpg://u:pw@db:5432/app"),
        (
            "postgresql://u:pw@db/app?sslmode=require",
            "postgresql+asyncpg://u:pw@db/app?ssl=require",
        ),
        (
            "postgresql+asyncpg://swordbot:swordbot@localhost:5432/swordbot",
            "postgresql+asyncpg://swordbot:swordbot@localhost:5432/swordbot",
        ),
        ("  postgresql://u:pw@db/app\n", "postgresql+asyncpg://u:pw@db/app"),
        ("sqlite+aiosqlite:///./swordbot.db", "sqlite+aiosqlite:///./swordbot.db"),
    ],
)
def test_database_url_is_normalized_for_asyncpg(given: str, expected: str) -> None:
    assert normalize_database_url(given) == expected
    assert Settings(_env_file=None, database_url=given).database_url == expected


def _production(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": Environment.PRODUCTION,
        "database_url": "postgresql://u:pw@db:5432/app",
        "app_base_url": "https://swordbot.example",
        "telegram_bot_token": SecretStr("123:abc"),
        "telegram_webhook_secret": SecretStr("webhook-secret"),
        "telegram_allowed_user_id": 1,
        "openai_api_key": SecretStr("sk-test"),
        "google_client_id": "id.apps.googleusercontent.com",
        "google_client_secret": SecretStr("client-secret"),
        "gmail_refresh_token": SecretStr("refresh"),
        "gmail_sender_address": "me@example.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_complete_production_settings_pass() -> None:
    assert production_config_errors(_production()) == []
    check_production_config(_production())


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"database_url": "sqlite+aiosqlite:///./swordbot.db"}, "DATABASE_URL"),
        ({"app_base_url": None}, "APP_BASE_URL"),
        ({"app_base_url": "http://swordbot.example"}, "APP_BASE_URL"),
        ({"telegram_webhook_secret": None}, "TELEGRAM_WEBHOOK_SECRET"),
        ({"telegram_allowed_user_id": None}, "TELEGRAM_ALLOWED_USER_ID"),
        # A blank `OPENAI_API_KEY=` line counts as missing.
        ({"openai_api_key": SecretStr("  ")}, "OPENAI_API_KEY"),
        ({"gmail_refresh_token": None}, "GMAIL_REFRESH_TOKEN"),
        ({"gmail_sender_address": ""}, "GMAIL_SENDER_ADDRESS"),
    ],
)
def test_missing_production_settings_are_named(overrides: dict[str, object], error: str) -> None:
    errors = production_config_errors(_production(**overrides))
    assert [e for e in errors if e.startswith(error)]
    with pytest.raises(ConfigurationError, match=error):
        check_production_config(_production(**overrides))


def test_the_error_names_settings_but_never_their_values() -> None:
    settings = _production(telegram_webhook_secret=None, openai_api_key=None)
    with pytest.raises(ConfigurationError) as info:
        check_production_config(settings)
    message = str(info.value)
    assert "123:abc" not in message
    assert "refresh" not in message


def test_development_does_not_require_production_settings() -> None:
    check_production_config(Settings(_env_file=None, environment=Environment.DEVELOPMENT))


async def test_production_app_refuses_to_start_without_its_settings() -> None:
    app = create_app(Settings(_env_file=None, environment=Environment.PRODUCTION))
    with pytest.raises(ConfigurationError):
        async with app.router.lifespan_context(app):
            pass

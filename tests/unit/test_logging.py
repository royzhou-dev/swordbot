import logging

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.logging import REDACTED, configure_logging, redact_sensitive

BOT_TOKEN = "123456789:AAFakeTokenFakeTokenFakeTokenFake12"


def test_redacts_sensitive_top_level_keys() -> None:
    out = redact_sensitive(
        None,
        "info",
        {
            "event": "x",
            "refresh_token": "abc",
            "Authorization": "Bearer abc",
            "email_body": "hello",
            "case_id": "c1",
        },
    )
    assert out["refresh_token"] == REDACTED
    assert out["Authorization"] == REDACTED
    assert out["email_body"] == REDACTED
    assert out["case_id"] == "c1"
    assert out["event"] == "x"


def test_redacts_nested_values() -> None:
    out = redact_sensitive(
        None,
        "info",
        {"event": "x", "request": {"headers": {"authorization": "Bearer abc"}, "path": "/p"}},
    )
    assert out["request"] == {"headers": {"authorization": REDACTED}, "path": "/p"}


def test_secret_str_config_values_do_not_leak_in_repr() -> None:
    from app.config import Settings

    s = Settings(_env_file=None, openai_api_key="sk-live-123")
    assert "sk-live-123" not in repr(s)


def test_bot_tokens_are_scrubbed_from_any_string_value() -> None:
    out = redact_sensitive(
        None,
        "info",
        {
            "event": "x",
            "url": f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            "nested": {"note": f"token {BOT_TOKEN} leaked"},
            "ids": ["12345", "chat 1000000001"],
        },
    )
    assert BOT_TOKEN not in str(out)
    assert out["url"] == f"https://api.telegram.org/bot{REDACTED}/sendMessage"
    # Plain numbers and ids are untouched.
    assert out["ids"] == ["12345", "chat 1000000001"]


def test_http_client_loggers_do_not_log_request_urls() -> None:
    configure_logging("DEBUG", json_output=False)
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


@pytest.mark.parametrize("secret", ["has space", "semi;colon", "x" * 257, ""])
def test_webhook_secret_must_use_telegrams_charset(secret: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, telegram_webhook_secret=secret)

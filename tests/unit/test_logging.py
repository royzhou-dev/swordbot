from app.logging import REDACTED, redact_sensitive


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

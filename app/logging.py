"""Structured logging setup.

Logs are JSON in production and human-readable in development. A redaction
processor masks values whose key looks sensitive (tokens, secrets, email bodies,
auth headers) so they never reach log output, even if a caller passes them by
mistake. It also blanks anything shaped like a Telegram bot token inside any
string value, because the token is part of every Bot API URL.
"""

import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"token|secret|password|passwd|api_key|apikey|authorization|cookie|credential"
    r"|^body$|_body$|^body_|^text$|^html$|card_number|cvv",
    re.IGNORECASE,
)

# "<bot id>:<35-character secret>", as issued by @BotFather.
_BOT_TOKEN = re.compile(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{30,}")

# These log full request URLs at INFO, and Telegram URLs contain the bot token.
_NOISY_URL_LOGGERS = ("httpx", "httpcore")


def _redact(value: Any) -> Any:
    if isinstance(value, MutableMapping):
        return {
            k: REDACTED if _SENSITIVE_KEY.search(str(k)) else _redact(v) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_redact(v) for v in value)
    if isinstance(value, str):
        return _BOT_TOKEN.sub(REDACTED, value)
    return value


def redact_sensitive(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key == "event":
            continue
        if _SENSITIVE_KEY.search(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact(event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_sensitive,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=level.upper(), format="%(message)s")
    for name in _NOISY_URL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger

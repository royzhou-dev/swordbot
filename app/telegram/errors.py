"""Typed Telegram Bot API errors.

Messages never contain the request URL, because the bot token is part of it.
Permanent errors subclass `PermanentEventError`, so an event that hits one goes
straight to `dead` instead of retrying.
"""

from app.events.errors import PermanentEventError


class TelegramError(Exception):
    """Base class for Telegram errors."""


class TelegramTemporaryError(TelegramError):
    """A network failure, timeout, 5xx or rate limit. Retrying may succeed."""


class TelegramRateLimitError(TelegramTemporaryError):
    def __init__(self, retry_after: int | None) -> None:
        super().__init__(f"rate limited (retry after {retry_after}s)")
        self.retry_after = retry_after


class TelegramPermanentError(TelegramError, PermanentEventError):
    """Retrying the same request cannot succeed."""


class TelegramAuthenticationError(TelegramPermanentError):
    """The bot token is missing, wrong or revoked."""


class TelegramRequestError(TelegramPermanentError):
    """Telegram rejected the request (400 Bad Request, 403 Forbidden, ...).

    `description` is Telegram's own error text, such as "Forbidden: bot was
    blocked by the user". It does not echo message content.
    """

    def __init__(self, error_code: int, description: str) -> None:
        super().__init__(f"{error_code}: {description}")
        self.error_code = error_code
        self.description = description

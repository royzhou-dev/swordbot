"""Typed LLM errors.

Messages carry only a status code, the provider's error code, or a schema name,
never prompt or output text: the worker stores the message of an `app.*`
exception in `events.last_error`. Permanent errors subclass `PermanentEventError`,
so an event that hits one goes straight to `dead` and the user is notified.
"""

from app.events.errors import PermanentEventError


class LLMError(Exception):
    """Base class for LLM errors."""


class LLMTemporaryError(LLMError):
    """A timeout, connection failure, 5xx or rate limit. Retrying may succeed."""


class LLMPermanentError(LLMError, PermanentEventError):
    """Retrying the same request cannot succeed."""


class LLMAuthenticationError(LLMPermanentError):
    """The API key is missing, wrong, revoked, or not allowed to use the model."""


class LLMQuotaError(LLMPermanentError):
    """The account is out of credit (429 `insufficient_quota` or `credit_balance_exhausted`).

    Needs billing, not a retry.
    """


class LLMRequestError(LLMPermanentError):
    """The provider rejected the request (400, 404 such as an unknown model, 422)."""

    def __init__(self, status_code: int, code: str | None) -> None:
        super().__init__(f"{status_code}: {code or 'request rejected'}")
        self.status_code = status_code
        self.code = code


class LLMRefusalError(LLMPermanentError):
    """The model refused to produce the requested output."""

    def __init__(self, schema_name: str) -> None:
        super().__init__(f"model refused to produce {schema_name}")
        self.schema_name = schema_name


class InvalidAgentDecisionError(LLMPermanentError):
    """Structured output failed validation even after the one repair retry.

    Raised for every structured output, not only agent decisions. `locations`
    lists where validation failed, never the values.
    """

    def __init__(self, schema_name: str, locations: list[str]) -> None:
        where = ", ".join(locations) if locations else "unknown"
        super().__init__(f"invalid {schema_name} output at: {where}")
        self.schema_name = schema_name
        self.locations = locations

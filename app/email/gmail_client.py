"""The Gmail boundary: sending, and (from M8) reading the mailbox (PLAN D4, D14, D16).

Sending is two calls, split around the send claim. `authorize` makes sure an
access token is ready and runs *before* the claim, so a token failure can be
retried with the email still approved. `send` runs after the claim; it must
raise the typed errors in `app.email.errors`, because the caller decides from
them whether the email certainly wasn't sent.

Reading (`search`, `get_message`) needs the `gmail.readonly` scope. `can_read`
says whether the token carries it, so a token from before M8 only switches
receipt search off and sending keeps working.

`HttpGmailClient` is the real client: a refresh-token grant against Google's
token endpoint and `users.messages.send`, both plain `httpx` calls (PLAN D6).
Without Gmail credentials the app uses `UnconfiguredGmailClient`, and tests
use `FakeGmailClient`.

Tokens never appear in errors or logs: errors carry the call, an HTTP status
and Google's short error code, and never chain the `httpx` exception.
"""

import asyncio
import base64
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from app.config import Settings
from app.email.errors import (
    GmailAuthenticationError,
    GmailRejectedError,
    GmailTemporaryError,
    GmailUnreachableError,
)
from app.logging import get_logger

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (a URL, not a secret)
GMAIL_API_BASE_URL = "https://gmail.googleapis.com"

_TOKEN_TIMEOUT = 15.0
_SEND_TIMEOUT = 30.0
_READ_TIMEOUT = 10.0
# Refresh this long before Google's expiry, so a token can't lapse between
# `authorize` and `send`.
_REFRESH_MARGIN_SECONDS = 300.0
_DEFAULT_TOKEN_LIFETIME_SECONDS = 3600.0

# The connection was never made, so the request can't have reached Google.
_NOT_CONNECTED = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# A 403 that means the account isn't set up for this call, not that the message was refused.
_SETUP_REASONS = frozenset({"insufficientPermissions", "accessNotConfigured", "authError"})
_ERROR_CODE = re.compile(r"[A-Za-z_]{1,64}")
# Gmail's message ids are hex. Anything else never goes into a URL.
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True, slots=True)
class GmailSent:
    """Gmail's ids for an accepted message."""

    message_id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class GmailRef:
    """A message in the mailbox, as a search lists it."""

    message_id: str
    thread_id: str


class GmailClient(Protocol):
    async def authorize(self) -> None:
        """Make sure a valid access token is ready. Sends nothing."""
        ...

    async def send(self, raw: bytes) -> GmailSent:
        """Send one RFC 822 message from the user's account."""
        ...

    async def can_read(self) -> bool:
        """Whether the mailbox can be searched and read (the `gmail.readonly` scope)."""
        ...

    async def search(self, query: str, *, max_results: int) -> list[GmailRef]:
        """The newest messages matching a Gmail search query."""
        ...

    async def get_message(self, message_id: str) -> dict[str, Any]:
        """One message as Gmail's `users.messages` resource (`format=full`)."""
        ...

    async def aclose(self) -> None: ...


class UnconfiguredGmailClient:
    """Used until Gmail is set up. Every send fails as unauthorized, so nothing is sent."""

    async def authorize(self) -> None:
        raise GmailAuthenticationError("Gmail is not connected (GMAIL_REFRESH_TOKEN is not set)")

    async def send(self, raw: bytes) -> GmailSent:
        raise GmailAuthenticationError("Gmail is not connected (GMAIL_REFRESH_TOKEN is not set)")

    async def can_read(self) -> bool:
        return False

    async def search(self, query: str, *, max_results: int) -> list[GmailRef]:
        raise GmailAuthenticationError("Gmail is not connected (GMAIL_REFRESH_TOKEN is not set)")

    async def get_message(self, message_id: str) -> dict[str, Any]:
        raise GmailAuthenticationError("Gmail is not connected (GMAIL_REFRESH_TOKEN is not set)")

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class _AccessToken:
    value: SecretStr
    # On the client's monotonic clock.
    refresh_after: float
    # The scopes Google says the token carries. None if it didn't say.
    scopes: frozenset[str] | None


class HttpGmailClient:
    """The real client. The caller owns `http` and closes it."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: SecretStr,
        refresh_token: SecretStr,
        http: httpx.AsyncClient,
        token_url: str = GOOGLE_TOKEN_URL,
        api_base_url: str = GMAIL_API_BASE_URL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._http = http
        self._token_url = token_url
        self._messages_url = f"{api_base_url.rstrip('/')}/gmail/v1/users/me/messages"
        self._send_url = f"{self._messages_url}/send"
        self._clock = clock
        self._token: _AccessToken | None = None
        # One refresh at a time, however many events want a token.
        self._lock = asyncio.Lock()

    async def authorize(self) -> None:
        await self._access_token()

    async def send(self, raw: bytes) -> GmailSent:
        try:
            token = await self._access_token()
        except GmailTemporaryError:
            # `authorize` just ran, so this is rare. Either way the send
            # endpoint was never called.
            raise GmailUnreachableError("send: no access token") from None
        try:
            response = await self._http.post(
                self._send_url,
                json={"raw": base64.urlsafe_b64encode(raw).decode("ascii")},
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                timeout=_SEND_TIMEOUT,
            )
        except _NOT_CONNECTED as exc:
            raise GmailUnreachableError(f"send: {type(exc).__name__}") from None
        except httpx.HTTPError as exc:
            # The request may have been sent: the outcome is unknown.
            raise GmailTemporaryError(f"send: {type(exc).__name__}") from None
        if response.is_success:
            return _parse_sent(response)
        status = response.status_code
        reason = _google_error_code(response)
        if status == 401:
            self._token = None
            raise GmailAuthenticationError(f"send: HTTP 401 {reason or ''}".rstrip())
        if status == 403 and reason in _SETUP_REASONS:
            raise GmailAuthenticationError(f"send: HTTP 403 {reason}")
        if 400 <= status < 500:
            # Gmail answered and refused, 429 included: nothing was sent.
            raise GmailRejectedError(status, reason)
        raise GmailTemporaryError(f"send: HTTP {status}")

    async def can_read(self) -> bool:
        scopes = (await self._access()).scopes
        # Google didn't list the scopes: assume so, and let a 403 say otherwise.
        return scopes is None or GMAIL_READ_SCOPE in scopes

    async def search(self, query: str, *, max_results: int) -> list[GmailRef]:
        body = await self._get(
            "search", self._messages_url, {"q": query, "maxResults": max_results}
        )
        messages = body.get("messages")
        if not isinstance(messages, list):
            return []
        return [
            GmailRef(message_id=message["id"], thread_id=message["threadId"])
            for message in messages
            if isinstance(message, dict)
            and isinstance(message.get("id"), str)
            and isinstance(message.get("threadId"), str)
        ]

    async def get_message(self, message_id: str) -> dict[str, Any]:
        if not _MESSAGE_ID.fullmatch(message_id):
            raise GmailRejectedError(400, "invalidMessageId")
        return await self._get("read", f"{self._messages_url}/{message_id}", {"format": "full"})

    async def _get(self, call: str, url: str, params: dict[str, str | int]) -> dict[str, Any]:
        """A read call. It changes nothing, so a temporary failure can simply be retried."""
        token = await self._access_token()
        try:
            response = await self._http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                timeout=_READ_TIMEOUT,
            )
        except _NOT_CONNECTED as exc:
            raise GmailUnreachableError(f"{call}: {type(exc).__name__}") from None
        except httpx.HTTPError as exc:
            raise GmailTemporaryError(f"{call}: {type(exc).__name__}") from None
        if response.is_success:
            return _json_object(response)
        status = response.status_code
        reason = _google_error_code(response)
        if status == 401:
            self._token = None
            raise GmailAuthenticationError(f"{call}: HTTP 401 {reason or ''}".rstrip())
        if status == 403 and reason in _SETUP_REASONS:
            # Most often a token from before M8, without the gmail.readonly scope.
            raise GmailAuthenticationError(f"{call}: HTTP 403 {reason}")
        if status == 429 or status >= 500:
            raise GmailTemporaryError(f"{call}: HTTP {status}")
        raise GmailRejectedError(status, reason)

    async def aclose(self) -> None:
        return None

    async def _access_token(self) -> SecretStr:
        return (await self._access()).value

    async def _access(self) -> _AccessToken:
        async with self._lock:
            if self._token is None or self._clock() >= self._token.refresh_after:
                self._token = await self._refresh()
            return self._token

    async def _refresh(self) -> _AccessToken:
        started = self._clock()
        try:
            response = await self._http.post(
                self._token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token.get_secret_value(),
                    "client_id": self._client_id,
                    "client_secret": self._client_secret.get_secret_value(),
                },
                timeout=_TOKEN_TIMEOUT,
            )
        except _NOT_CONNECTED as exc:
            raise GmailUnreachableError(f"token refresh: {type(exc).__name__}") from None
        except httpx.HTTPError as exc:
            raise GmailTemporaryError(f"token refresh: {type(exc).__name__}") from None
        body = _json_object(response)
        if response.status_code >= 500 or response.status_code == 429:
            raise GmailTemporaryError(f"token refresh: HTTP {response.status_code}")
        if not response.is_success:
            # invalid_grant (revoked, or expired in Testing mode), invalid_client, ...
            code = _short_code(body.get("error")) or f"HTTP {response.status_code}"
            raise GmailAuthenticationError(f"token refresh: {code}")
        value = body.get("access_token")
        if not isinstance(value, str) or not value:
            raise GmailTemporaryError("token refresh: no access token in the response")
        scope = body.get("scope")
        scopes = frozenset(scope.split()) if isinstance(scope, str) else None
        if scopes is not None and GMAIL_SEND_SCOPE not in scopes:
            raise GmailAuthenticationError("token refresh: the token lacks the gmail.send scope")
        expires_in = body.get("expires_in")
        lifetime = (
            float(expires_in)
            if isinstance(expires_in, int | float) and expires_in > 0
            else _DEFAULT_TOKEN_LIFETIME_SECONDS
        )
        return _AccessToken(
            value=SecretStr(value),
            refresh_after=started + max(lifetime - _REFRESH_MARGIN_SECONDS, 0.0),
            scopes=scopes,
        )


def build_gmail_client(settings: Settings, http: httpx.AsyncClient) -> GmailClient:
    """The real client when all three credentials are set, else `UnconfiguredGmailClient`."""
    client_id = (settings.google_client_id or "").strip()
    secret = _present(settings.google_client_secret)
    refresh = _present(settings.gmail_refresh_token)
    if not client_id or secret is None or refresh is None:
        get_logger(__name__).warning("gmail_not_configured")
        return UnconfiguredGmailClient()
    return HttpGmailClient(
        client_id=client_id, client_secret=secret, refresh_token=refresh, http=http
    )


def _present(value: SecretStr | None) -> SecretStr | None:
    """A blank `.env` line counts as unset."""
    if value is None or not value.get_secret_value().strip():
        return None
    return SecretStr(value.get_secret_value().strip())


def _parse_sent(response: httpx.Response) -> GmailSent:
    body = _json_object(response)
    message_id, thread_id = body.get("id"), body.get("threadId")
    if not isinstance(message_id, str) or not isinstance(thread_id, str):
        # Gmail said yes, but we can't tell which message: treat the outcome as unknown.
        raise GmailTemporaryError("send: unexpected response")
    return GmailSent(message_id=message_id, thread_id=thread_id)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _google_error_code(response: httpx.Response) -> str | None:
    """Google's short error code (`errors[0].reason`, else `status`). Never the message text."""
    error = _json_object(response).get("error")
    if not isinstance(error, dict):
        return None
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = _short_code(errors[0].get("reason"))
        if reason:
            return reason
    return _short_code(error.get("status"))


def _short_code(value: object) -> str | None:
    return value if isinstance(value, str) and _ERROR_CODE.fullmatch(value) else None

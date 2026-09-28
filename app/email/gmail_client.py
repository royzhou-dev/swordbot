"""The Gmail boundary: what the send path needs from Gmail (PLAN D4, D14).

Two calls, split around the send claim. `authorize` makes sure an access token
is ready and runs *before* the claim, so a token failure can be retried with
the email still approved. `send` runs after the claim; it must raise the
typed errors in `app.email.errors`, because the caller decides from them
whether the email certainly wasn't sent.

M7 part 2 adds the real `httpx` client (token refresh with `google-auth`,
`users.messages.send`) and `scripts/gmail_auth.py`. Until then the app uses
`UnconfiguredGmailClient`, and tests use `FakeGmailClient`.
"""

from dataclasses import dataclass
from typing import Protocol

from app.email.errors import GmailAuthenticationError


@dataclass(frozen=True, slots=True)
class GmailSent:
    """Gmail's ids for an accepted message."""

    message_id: str
    thread_id: str


class GmailClient(Protocol):
    async def authorize(self) -> None:
        """Make sure a valid access token is ready. Sends nothing."""
        ...

    async def send(self, raw: bytes) -> GmailSent:
        """Send one RFC 822 message from the user's account."""
        ...

    async def aclose(self) -> None: ...


class UnconfiguredGmailClient:
    """Used until Gmail is set up. Every send fails as unauthorized, so nothing is sent."""

    async def authorize(self) -> None:
        raise GmailAuthenticationError("Gmail is not connected (GMAIL_REFRESH_TOKEN is not set)")

    async def send(self, raw: bytes) -> GmailSent:
        raise GmailAuthenticationError("Gmail is not connected (GMAIL_REFRESH_TOKEN is not set)")

    async def aclose(self) -> None:
        return None

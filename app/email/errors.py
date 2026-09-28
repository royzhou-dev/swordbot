"""Email errors. Messages carry ids and statuses, never addresses or content."""

import uuid

from app.email.models import OutboundEmailStatus
from app.events.errors import PermanentEventError


class EmailError(PermanentEventError):
    """Base class for outbound-email errors."""


class DraftLockedError(EmailError):
    """The case's email is past approval (approved or being sent) and can't be replaced here."""

    def __init__(self, case_id: uuid.UUID, status: OutboundEmailStatus) -> None:
        super().__init__(f"case {case_id}: its email is {status.value} and can't be changed")
        self.case_id = case_id
        self.status = status


class UnknownEmailError(EmailError):
    """An event names an email that doesn't exist or belongs to another user."""

    def __init__(self, email_id: uuid.UUID) -> None:
        super().__init__(f"outbound email {email_id} not found")
        self.email_id = email_id


# --- Gmail (PLAN D2, D14) ------------------------------------------------------------
#
# What matters after a send is claimed is whether Gmail can have received the
# message. Only errors that prove it didn't (a refusal, bad credentials, a
# connection that was never made) let the send be marked `failed`. Anything
# else leaves the outcome unknown, and the email goes to `needs_attention`.


class GmailError(Exception):
    """Base class for Gmail errors. Messages never contain tokens or email content."""


class GmailTemporaryError(GmailError):
    """A timeout, 5xx or dropped connection. From a send, the outcome is unknown."""


class GmailUnreachableError(GmailTemporaryError):
    """The connection to Gmail was never made, so nothing was sent."""


class GmailPermanentError(GmailError, PermanentEventError):
    """Gmail answered, and retrying the same request can't succeed. Nothing was sent."""


class GmailAuthenticationError(GmailPermanentError):
    """No refresh token, Google rejected it (revoked, expired, wrong scope), or the
    account isn't set up for the call (e.g. the Gmail API isn't enabled)."""


class GmailRejectedError(GmailPermanentError):
    """Gmail refused the message (a 4xx response other than authentication)."""

    def __init__(self, status_code: int, reason: str | None = None) -> None:
        super().__init__(f"{status_code}: {reason or 'rejected'}")
        self.status_code = status_code
        self.reason = reason


def provably_not_sent(exc: BaseException) -> bool:
    """Whether a failed send certainly did not reach Gmail."""
    return isinstance(exc, GmailPermanentError | GmailUnreachableError)


class NoSupportAddressError(EmailError):
    """A draft was requested for a case with no support email address.

    Intake requires the address before drafting, so this is a code bug.
    """

    def __init__(self, case_id: uuid.UUID) -> None:
        super().__init__(f"case {case_id} has no support email address")
        self.case_id = case_id

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


class NoSupportAddressError(EmailError):
    """A draft was requested for a case with no support email address.

    Intake requires the address before drafting, so this is a code bug.
    """

    def __init__(self, case_id: uuid.UUID) -> None:
        super().__init__(f"case {case_id} has no support email address")
        self.case_id = case_id

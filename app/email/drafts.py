"""Creating, approving and discarding email drafts (PLAN D2).

Status changes are conditional UPDATEs (`WHERE status = <expected>`), so two
events racing on one email can't both succeed. In particular `approve` only
succeeds while the email is awaiting approval and its content still has the
hash the Send button was bound to: a double press approves once, and a Send
button from an older version approves nothing.

Functions take the caller's session and never commit.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.models import SupportCase
from app.db.session import rowcount
from app.email.errors import DraftLockedError
from app.email.models import LIVE_STATUSES, OutboundEmail, OutboundEmailStatus
from app.logging import get_logger

log = get_logger(__name__)

S = OutboundEmailStatus
SIGN_OFF = "Thank you,"


def content_hash(to_address: str, subject: str, body: str) -> str:
    """The hash an approval is bound to. Any change to recipient, subject or body changes it."""
    canonical = json.dumps([to_address, subject, body], ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compose_body(body_text: str, signature_name: str | None) -> str:
    """The drafted body plus the sign-off. Code signs the email, not the model."""
    closing = f"{SIGN_OFF}\n{signature_name}" if signature_name else SIGN_OFF
    return f"{body_text.strip()}\n\n{closing}"


@dataclass(frozen=True, slots=True)
class NewVersion:
    email: OutboundEmail
    # The version it replaced, now superseded.
    replaced: OutboundEmail | None


async def live_email(session: AsyncSession, case: SupportCase) -> OutboundEmail | None:
    """The case's email that is awaiting approval, approved or being sent, if any."""
    return await session.scalar(
        select(OutboundEmail).where(
            OutboundEmail.case_id == case.id, OutboundEmail.status.in_(LIVE_STATUSES)
        )
    )


async def create_version(
    session: AsyncSession,
    case: SupportCase,
    *,
    to_address: str,
    subject: str,
    body_text: str,
    signature_name: str | None,
    event_id: int | None,
) -> NewVersion:
    """Create the next version of the case's email, awaiting approval.

    A version still awaiting approval is superseded. One that is already
    approved or sending can't be replaced (`DraftLockedError`).
    """
    replaced = await live_email(session, case)
    if replaced is not None:
        if replaced.status is not S.AWAITING_APPROVAL:
            raise DraftLockedError(case.id, replaced.status)
        if not await _move(session, replaced, S.AWAITING_APPROVAL, S.SUPERSEDED):
            raise DraftLockedError(case.id, replaced.status)

    latest = await session.scalar(
        select(func.max(OutboundEmail.version)).where(OutboundEmail.case_id == case.id)
    )
    body = compose_body(body_text, signature_name)
    email = OutboundEmail(
        id=uuid.uuid4(),
        user_id=case.user_id,
        case_id=case.id,
        version=(latest or 0) + 1,
        supersedes_id=replaced.id if replaced else None,
        status=S.AWAITING_APPROVAL,
        to_address=to_address,
        subject=subject,
        body=body,
        body_text=body_text.strip(),
        signature_name=signature_name,
        content_hash=content_hash(to_address, subject, body),
        created_by_event_id=event_id,
    )
    session.add(email)
    await session.flush()
    # Never the content: ids and the version only.
    log.info(
        "email_draft_created",
        case_id=str(case.id),
        outbound_email_id=str(email.id),
        version=email.version,
    )
    return NewVersion(email, replaced)


async def attach_buttons(session: AsyncSession, email: OutboundEmail, group_id: uuid.UUID) -> None:
    """Record the button group now shown for this version."""
    email.action_group_id = group_id
    await session.flush()


async def approve(
    session: AsyncSession,
    email_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    expected_hash: str,
    event_id: int,
    action_id: uuid.UUID,
    now: datetime,
) -> OutboundEmail | None:
    """Approve exactly the content with `expected_hash`. None if that isn't possible.

    One conditional UPDATE: the email must belong to the user, still await
    approval, and still have the content the Send button was shown with.
    """
    result = await session.execute(
        update(OutboundEmail)
        .where(
            OutboundEmail.id == email_id,
            OutboundEmail.user_id == user_id,
            OutboundEmail.status == S.AWAITING_APPROVAL,
            OutboundEmail.content_hash == expected_hash,
        )
        .values(
            status=S.APPROVED,
            approved_at=now,
            approved_by_event_id=event_id,
            approved_by_action_id=action_id,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    approved = rowcount(result) == 1
    log.info("email_approval", outbound_email_id=str(email_id), approved=approved)
    if not approved:
        return None
    email = await session.get(OutboundEmail, email_id)
    if email is not None:
        await session.refresh(email)
    return email


async def discard_live(
    session: AsyncSession, case: SupportCase, *, to_status: OutboundEmailStatus
) -> OutboundEmail | None:
    """Supersede or cancel the case's live email, if it hasn't started sending.

    Returns the discarded email, or None if there was none. An email that is
    sending can't be discarded (`DraftLockedError`); M7 decides what happens.
    """
    if to_status not in (S.SUPERSEDED, S.CANCELLED):
        raise ValueError(f"cannot discard an email as {to_status.value}")
    email = await live_email(session, case)
    if email is None:
        return None
    if email.status is S.SENDING or not await _move(session, email, email.status, to_status):
        raise DraftLockedError(case.id, email.status)
    log.info(
        "email_draft_discarded",
        case_id=str(case.id),
        outbound_email_id=str(email.id),
        status=to_status.value,
    )
    return email


async def _move(
    session: AsyncSession,
    email: OutboundEmail,
    from_status: OutboundEmailStatus,
    to_status: OutboundEmailStatus,
    **values: Any,
) -> bool:
    """Conditionally change the email's status. False if it wasn't `from_status` any more."""
    result = await session.execute(
        update(OutboundEmail)
        .where(OutboundEmail.id == email.id, OutboundEmail.status == from_status)
        .values(status=to_status, **values)
        .execution_options(synchronize_session=False)
    )
    await session.refresh(email)
    return rowcount(result) == 1

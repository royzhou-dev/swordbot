"""`outbound_emails`: every email the assistant may send, and the send guard (PLAN D2).

Each row is one version of one email. Its content (`to_address`, `subject`,
`body`) never changes after creation; an edit creates the next version and
supersedes this one. An approval is bound to `content_hash`, a hash of exactly
that content, so approving one version can never authorize another.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import LOG_ID, Base, StrEnumType, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin


class OutboundEmailStatus(StrEnum):
    # Shown to the user with [Send] [Edit] [Cancel].
    AWAITING_APPROVAL = "awaiting_approval"
    # The user pressed Send for exactly this content.
    APPROVED = "approved"
    # M7: claimed for sending by an atomic conditional update. Never re-sent automatically.
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    # M7: a send crashed midway; the user decides what happens.
    NEEDS_ATTENTION = "needs_attention"
    # Replaced by a later version, or discarded because the case went back to intake.
    SUPERSEDED = "superseded"
    # The case was cancelled before the email was sent.
    CANCELLED = "cancelled"


# At most one email per case is in one of these statuses (a partial unique index).
LIVE_STATUSES = frozenset(
    {
        OutboundEmailStatus.AWAITING_APPROVAL,
        OutboundEmailStatus.APPROVED,
        OutboundEmailStatus.SENDING,
    }
)

_LIVE_WHERE = text("status IN ('awaiting_approval', 'approved', 'sending')")


class OutboundEmail(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One version of an outbound email. Changed only through `app.email.drafts`."""

    __tablename__ = "outbound_emails"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("support_cases.id"))
    # 1, 2, ... within the case.
    version: Mapped[int] = mapped_column(Integer)
    # The version this one replaced.
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("outbound_emails.id"))
    status: Mapped[OutboundEmailStatus] = mapped_column(StrEnumType(OutboundEmailStatus))

    to_address: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(200))
    # What is sent: `body_text` plus the sign-off that code adds.
    body: Mapped[str] = mapped_column(Text)
    # The body as drafted, before the sign-off. Kept so the email can be
    # re-signed or re-addressed without asking the model to rewrite it.
    body_text: Mapped[str] = mapped_column(Text)
    # The name in the sign-off, if any.
    signature_name: Mapped[str | None] = mapped_column(String(200))
    # sha256 of (to_address, subject, body). What an approval is bound to.
    content_hash: Mapped[str] = mapped_column(String(64))

    # The event whose handler created this version (audit trail).
    created_by_event_id: Mapped[int | None] = mapped_column(LOG_ID, ForeignKey("events.id"))
    # The [Send] [Edit] [Cancel] buttons currently shown for this version.
    action_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    approved_by_event_id: Mapped[int | None] = mapped_column(LOG_ID, ForeignKey("events.id"))
    # The consumed Send button: the approval record the send tool checks (M7).
    approved_by_action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pending_actions.id")
    )

    __table_args__ = (
        UniqueConstraint("case_id", "version"),
        Index(
            "uq_outbound_emails_one_live_per_case",
            "case_id",
            unique=True,
            postgresql_where=_LIVE_WHERE,
            sqlite_where=_LIVE_WHERE,
        ),
    )

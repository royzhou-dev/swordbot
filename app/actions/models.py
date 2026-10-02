"""`pending_actions`: the durable record behind every chat button (PLAN D3).

A button carries only its action's id. Pressing it is validated against this
row (open, same user, not expired, case still in the expected status) and
consumes it. Buttons shown together share a `group_id`; consuming one closes
the rest, so a prompt can be answered once.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.cases.models import CaseStatus
from app.db.base import LOG_ID, Base, StrEnumType, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin


class ActionKind(StrEnum):
    """What pressing the button does. Members are added as milestones need them."""

    # The /cancel confirmation: cancel the case, or leave it alone.
    CANCEL_CASE = "cancel_case"
    KEEP_CASE = "keep_case"
    # Under an email draft (M6). The payload names the email and its content hash.
    SEND_EMAIL = "send_email"
    EDIT_DRAFT = "edit_draft"
    CANCEL_DRAFT = "cancel_draft"
    # After a send whose outcome is unknown (M7): the user checked Gmail's Sent folder.
    CONFIRM_SENT = "confirm_sent"
    CONFIRM_NOT_SENT = "confirm_not_sent"
    # Under an order found in Gmail (M8): is this the order? The payload holds
    # the receipt's details, which become case facts only on Yes.
    CONFIRM_RECEIPT = "confirm_receipt"
    REJECT_RECEIPT = "reject_receipt"
    # A support address found in a confirmed receipt: write to it, or not.
    USE_RECEIPT_CONTACT = "use_receipt_contact"
    SKIP_RECEIPT_CONTACT = "skip_receipt_contact"


class ActionStatus(StrEnum):
    OPEN = "open"
    # This button was pressed.
    CONSUMED = "consumed"
    # Another button in the group was pressed, or the prompt no longer applies.
    SUPERSEDED = "superseded"


class PendingAction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One button. Changed only through `app.actions.service`."""

    __tablename__ = "pending_actions"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("support_cases.id"))
    group_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # Order within the group, left to right.
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[ActionKind] = mapped_column(StrEnumType(ActionKind))
    label: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[ActionStatus] = mapped_column(StrEnumType(ActionStatus, length=16))
    # When set, the action is only valid while its case is in this status.
    expected_case_status: Mapped[CaseStatus | None] = mapped_column(StrEnumType(CaseStatus))
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # The button-press event that consumed it (audit trail).
    consumed_by_event_id: Mapped[int | None] = mapped_column(LOG_ID, ForeignKey("events.id"))
    # Where the buttons were shown, once delivered.
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        Index("ix_pending_actions_group_id", "group_id"),
        Index("ix_pending_actions_user_id_status", "user_id", "status"),
    )

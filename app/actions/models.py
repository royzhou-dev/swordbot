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

    # M3 test button on the echo reply. Removed when M5 replaces the echo.
    ECHO_TEST = "echo_test"


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

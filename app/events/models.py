"""The `events` table: durable inbox for external inputs and queue for scheduled work.

See PLAN D1. Adapters insert rows (deduplicated on `(source, external_id)`),
and the worker claims and processes them one at a time per user.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import LOG_ID, Base, StrEnumType, TimestampMixin, UTCDateTime, utcnow


class EventType(StrEnum):
    """What happened. Members are added as milestones need them; no migration is required."""

    USER_MESSAGE = "user_message"
    USER_BUTTON_ACTION = "user_button_action"
    EMAIL_RECEIVED = "email_received"
    FOLLOW_UP_DUE = "follow_up_due"
    # One outbound Telegram API call, queued by a handler (PLAN D10).
    TELEGRAM_OUTBOUND = "telegram_outbound"
    # Draft a case's email once intake has everything (M6). Its own event, so a
    # drafting failure retries only the drafting.
    DRAFT_EMAIL = "draft_email"
    # Send an approved email (M7). Its own event, queued by the Send press, so
    # its claim can commit before Gmail is called (PLAN D14).
    SEND_EMAIL = "send_email"
    # Look for a case's order receipt in Gmail and read one candidate (M8).
    # One model call per event, so each stays within the handler's time limit.
    SEARCH_RECEIPTS = "search_receipts"


class EventSource(StrEnum):
    """Where the event came from. Together with `external_id`, it identifies a delivery."""

    TELEGRAM = "telegram"
    GMAIL = "gmail"
    SYSTEM = "system"
    # Synthetic events pushed by `scripts/inject_event.py` during development.
    DEV = "dev"


class EventStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    # Permanently failed or out of retries. Never retried automatically; the user is notified.
    DEAD = "dead"


class Event(TimestampMixin, Base):
    """One unit of work. Changed only through `app.events.service`."""

    __tablename__ = "events"

    # An integer id gives events a total order, which the worker uses to process
    # each user's events strictly in arrival order (PLAN D9).
    id: Mapped[int] = mapped_column(LOG_ID, primary_key=True, autoincrement=True)
    # Also the serialization key: a user's events never run concurrently.
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    # Unknown until the handler routes the event to a case.
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("support_cases.id"))
    type: Mapped[EventType] = mapped_column(StrEnumType(EventType))
    source: Mapped[EventSource] = mapped_column(StrEnumType(EventSource, length=16))
    # The source's own id for the delivery (Telegram update_id, Gmail message id,
    # or a deterministic key for scheduled work).
    external_id: Mapped[str] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)

    status: Mapped[EventStatus] = mapped_column(StrEnumType(EventStatus, length=16))
    # Incremented when the event is claimed, so a crash mid-handler still counts.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    run_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # The claim lease. `claim_token` fences off a worker whose lease expired.
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # Error type and a truncated message from the last failure. Not logged.
    last_error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_events_source_external_id"),
        Index("ix_events_status_run_at", "status", "run_at"),
        Index("ix_events_user_id_status", "user_id", "status"),
        # At most one event per user is processing. The claim query already
        # avoids this; the index makes it impossible even under a race.
        Index(
            "uq_events_one_processing_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'processing'"),
            sqlite_where=text("status = 'processing'"),
        ),
    )

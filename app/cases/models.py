"""Support case persistence: the case itself, its facts, and its transition log."""

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    LOG_ID,
    Base,
    StrEnumType,
    TimestampMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
    utcnow,
)


class CaseStatus(StrEnum):
    GATHERING_CONTEXT = "gathering_context"
    READY_TO_DRAFT = "ready_to_draft"
    WAITING_FOR_USER_APPROVAL = "waiting_for_user_approval"
    READY_TO_SEND = "ready_to_send"
    WAITING_FOR_SUPPORT = "waiting_for_support"
    PROCESSING_SUPPORT_REPLY = "processing_support_reply"
    WAITING_FOR_USER = "waiting_for_user"
    READY_TO_REPLY = "ready_to_reply"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"
    ERROR = "error"


class FactSource(StrEnum):
    """Where a case fact came from (Invariant 1: every fact carries provenance)."""

    USER_MESSAGE = "user_message"
    USER_CONFIRMATION = "user_confirmation"
    GMAIL_RECEIPT = "gmail_receipt"
    SUPPORT_EMAIL = "support_email"
    MERCHANT_WEBSITE = "merchant_website"
    WEB_SEARCH = "web_search"


class TransitionActor(StrEnum):
    """Who caused a state transition, for the audit log."""

    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class SupportCase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One support issue with one merchant/order.

    `status` changes only through `app.cases.state_machine.transition`. The
    fact-backed columns (see `app.cases.service.FACT_COLUMNS`) change only
    through `set_fact`, which records provenance in `case_facts`.
    """

    __tablename__ = "support_cases"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[CaseStatus] = mapped_column(StrEnumType(CaseStatus))

    # Fact-backed: the current accepted value of the matching case fact.
    merchant_name: Mapped[str | None] = mapped_column(String(200))
    merchant_domain: Mapped[str | None] = mapped_column(String(253))
    issue_type: Mapped[str | None] = mapped_column(String(64))
    issue_summary: Mapped[str | None] = mapped_column(Text)
    desired_resolution: Mapped[str | None] = mapped_column(Text)
    order_number: Mapped[str | None] = mapped_column(String(128))
    order_date: Mapped[date | None] = mapped_column(Date)
    support_email: Mapped[str | None] = mapped_column(String(320))

    # Operational.
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64), index=True)
    auto_reply_enabled: Mapped[bool] = mapped_column(default=False, server_default=false())
    # The case that plain chat messages are routed to (PLAN D7). At most one per user.
    focused: Mapped[bool] = mapped_column(default=False, server_default=false())
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Optimistic locking: SQLAlchemy bumps this on every UPDATE and raises
    # StaleDataError if the row changed underneath us.
    version: Mapped[int] = mapped_column(Integer)

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
    __table_args__ = (
        Index("ix_support_cases_user_id_status", "user_id", "status"),
        Index(
            "uq_support_cases_one_focused_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("focused"),
            sqlite_where=text("focused"),
        ),
    )


class CaseFact(Base):
    """One observation of a case fact. Append-only; the latest row per key is current."""

    __tablename__ = "case_facts"

    id: Mapped[int] = mapped_column(LOG_ID, primary_key=True, autoincrement=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("support_cases.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    key: Mapped[str] = mapped_column(String(64))
    # Any JSON value. JSON null means the fact was cleared.
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    source: Mapped[FactSource] = mapped_column(StrEnumType(FactSource))
    # Pointer to the evidence: a Telegram message id, Gmail message id, URL, ...
    source_ref: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_case_facts_case_id_key", "case_id", "key"),)


class CaseTransition(Base):
    """Audit log of every status change (Invariant 8)."""

    __tablename__ = "case_transitions"

    id: Mapped[int] = mapped_column(LOG_ID, primary_key=True, autoincrement=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("support_cases.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    # NULL for the row written when the case is created.
    from_status: Mapped[CaseStatus | None] = mapped_column(StrEnumType(CaseStatus))
    to_status: Mapped[CaseStatus] = mapped_column(StrEnumType(CaseStatus))
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[TransitionActor] = mapped_column(StrEnumType(TransitionActor, length=16))
    # The event that caused the transition, if any.
    event_id: Mapped[int | None] = mapped_column(LOG_ID, ForeignKey("events.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

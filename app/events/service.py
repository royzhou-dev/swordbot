"""The event queue: enqueue with deduplication, claim, complete, fail, and recover.

Every function takes the caller's session and never commits. The worker runs
each claim and each handler in its own transaction (see `app.events.worker`).

Ordering and serialization (PLAN D1): a user's events run one at a time, in
`id` order. An older event that is due, or waiting to retry, holds back newer
ones for the same user. An event scheduled for the future blocks nothing until
it is due. A partial unique index allows at most one `processing` event per
user, so a race between two claimers fails instead of running both.

Database-specific code is limited to two statements: the deduplicating insert
(`ON CONFLICT DO NOTHING`, supported by both Postgres and SQLite) and the claim
(`FOR UPDATE SKIP LOCKED` on Postgres; SQLite ignores it and runs one worker).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import Settings
from app.db.base import utcnow
from app.db.session import rowcount
from app.events.errors import LostClaimError
from app.events.models import Event, EventStatus
from app.events.schemas import ClaimedEvent, NewEvent
from app.logging import get_logger

log = get_logger(__name__)

_MAX_ERROR_LENGTH = 1000


@dataclass(frozen=True, slots=True)
class EventPolicy:
    """Retry and lease settings. The defaults retry for about 30 minutes."""

    max_attempts: int = 8
    retry_base: timedelta = timedelta(seconds=15)
    retry_max: timedelta = timedelta(minutes=30)
    # How long a claim lasts. A handler is cut off before its lease runs out.
    lease: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.lease <= timedelta(0):
            raise ValueError("lease must be positive")

    @property
    def handler_timeout(self) -> timedelta:
        return self.lease * 0.9

    @classmethod
    def from_settings(cls, settings: Settings) -> "EventPolicy":
        return cls(
            max_attempts=settings.event_max_attempts,
            retry_base=timedelta(seconds=settings.event_retry_base_seconds),
            retry_max=timedelta(seconds=settings.event_retry_max_seconds),
            lease=timedelta(seconds=settings.event_lease_seconds),
        )


def backoff_delay(attempt: int, policy: EventPolicy) -> timedelta:
    """Delay before retrying after failed attempt number `attempt` (1-based)."""
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    # Cap the exponent too, so a large attempt count can't overflow.
    return min(policy.retry_base * (1 << min(attempt - 1, 32)), policy.retry_max)


def describe_error(exc: BaseException) -> str:
    """A short description of a failure, safe to store in `events.last_error`.

    Only the app's own exceptions include their message, because third-party
    messages can contain URLs with tokens or echoed user content.
    """
    name = type(exc).__name__
    if type(exc).__module__.startswith("app.") and str(exc):
        return f"{name}: {exc}"[:_MAX_ERROR_LENGTH]
    return name


def _snapshot(event: Event, *, attempts: int, claim_token: uuid.UUID) -> ClaimedEvent:
    return ClaimedEvent(
        id=event.id,
        user_id=event.user_id,
        case_id=event.case_id,
        type=event.type,
        source=event.source,
        external_id=event.external_id,
        payload=dict(event.payload),
        attempts=attempts,
        claim_token=claim_token,
        created_at=event.created_at,
    )


async def enqueue(
    session: AsyncSession, new_event: NewEvent, *, now: datetime | None = None
) -> int | None:
    """Insert an event and return its id, or None if `(source, external_id)` already exists.

    A duplicate is a no-op (PLAN D1, Invariant 5). Other integrity errors, such
    as an unknown user, still raise. Also used to schedule work: pass a future
    `run_at`.
    """
    now = now or utcnow()
    values = {
        "user_id": new_event.user_id,
        "case_id": new_event.case_id,
        "type": new_event.type,
        "source": new_event.source,
        "external_id": new_event.external_id,
        "payload": new_event.payload,
        "status": EventStatus.PENDING,
        "attempts": 0,
        "run_at": new_event.run_at or now,
        "created_at": now,
        "updated_at": now,
    }
    dialect = session.get_bind().dialect.name
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    stmt = (
        insert(Event)
        .values(values)
        .on_conflict_do_nothing(index_elements=["source", "external_id"])
        .returning(Event.id)
    )
    event_id = await session.scalar(stmt)
    if event_id is None:
        log.info(
            "event_duplicate",
            source=new_event.source.value,
            type=new_event.type.value,
            user_id=str(new_event.user_id),
        )
        return None
    log.info(
        "event_enqueued",
        event_id=event_id,
        source=new_event.source.value,
        type=new_event.type.value,
        user_id=str(new_event.user_id),
        case_id=str(new_event.case_id) if new_event.case_id else None,
        scheduled=new_event.run_at is not None and new_event.run_at > now,
    )
    return event_id


async def claim_next(
    session: AsyncSession, *, now: datetime, policy: EventPolicy
) -> ClaimedEvent | None:
    """Claim the next runnable event, or return None if nothing can run now.

    Under a race with another claimer this may raise `IntegrityError` (from the
    one-processing-per-user index). The caller rolls back and tries again later.
    """
    other = aliased(Event)
    blocked = exists().where(
        other.user_id == Event.user_id,
        other.id != Event.id,
        or_(
            other.status == EventStatus.PROCESSING,
            and_(
                other.id < Event.id,
                other.status == EventStatus.PENDING,
                or_(other.run_at <= now, other.attempts > 0),
            ),
        ),
    )
    stmt = (
        select(Event)
        .where(Event.status == EventStatus.PENDING, Event.run_at <= now, ~blocked)
        .order_by(Event.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=Event)
        # Rows are changed with Core UPDATEs, so never trust a cached copy.
        .execution_options(populate_existing=True)
    )
    event = await session.scalar(stmt)
    if event is None:
        return None

    token = uuid.uuid4()
    attempts = event.attempts + 1
    result = await session.execute(
        update(Event)
        .where(Event.id == event.id, Event.status == EventStatus.PENDING)
        .values(
            status=EventStatus.PROCESSING,
            attempts=attempts,
            claim_token=token,
            locked_until=now + policy.lease,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if rowcount(result) == 0:
        return None
    return _snapshot(event, attempts=attempts, claim_token=token)


async def complete(session: AsyncSession, event: ClaimedEvent, *, now: datetime) -> None:
    """Mark a claimed event done. Raises `LostClaimError` if the claim is no longer ours."""
    result = await session.execute(
        update(Event)
        .where(
            Event.id == event.id,
            Event.claim_token == event.claim_token,
            Event.status == EventStatus.PROCESSING,
        )
        .values(
            status=EventStatus.DONE,
            claim_token=None,
            locked_until=None,
            processed_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if rowcount(result) == 0:
        raise LostClaimError(event.id)


async def fail(
    session: AsyncSession,
    event: ClaimedEvent,
    error: BaseException | str,
    *,
    permanent: bool,
    now: datetime,
    policy: EventPolicy,
) -> EventStatus:
    """Record a failed attempt: reschedule with backoff, or mark `dead`.

    The event is dead if the error is permanent or it has used all its
    attempts. Returns the new status. Raises `LostClaimError` if the claim is no
    longer ours.
    """
    dead = permanent or event.attempts >= policy.max_attempts
    description = error if isinstance(error, str) else describe_error(error)
    changes: dict[str, object] = {
        "claim_token": None,
        "locked_until": None,
        "last_error": description[:_MAX_ERROR_LENGTH],
        "updated_at": now,
    }
    if dead:
        changes |= {"status": EventStatus.DEAD, "processed_at": now}
    else:
        changes |= {
            "status": EventStatus.PENDING,
            "run_at": now + backoff_delay(event.attempts, policy),
        }
    result = await session.execute(
        update(Event)
        .where(
            Event.id == event.id,
            Event.claim_token == event.claim_token,
            Event.status == EventStatus.PROCESSING,
        )
        .values(changes)
        .execution_options(synchronize_session=False)
    )
    if rowcount(result) == 0:
        raise LostClaimError(event.id)
    return EventStatus.DEAD if dead else EventStatus.PENDING


async def release(session: AsyncSession, event: ClaimedEvent, *, now: datetime) -> None:
    """Hand a claimed event back without counting the attempt (graceful shutdown).

    Does nothing if the claim is no longer ours.
    """
    await session.execute(
        update(Event)
        .where(
            Event.id == event.id,
            Event.claim_token == event.claim_token,
            Event.status == EventStatus.PROCESSING,
        )
        .values(
            status=EventStatus.PENDING,
            attempts=Event.attempts - 1,
            claim_token=None,
            locked_until=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )


async def recover_expired(
    session: AsyncSession, *, now: datetime, policy: EventPolicy
) -> list[tuple[ClaimedEvent, EventStatus]]:
    """Fail every event whose lease expired, because its worker crashed or hung.

    Each counts as a failed attempt: it is rescheduled with backoff, or marked
    `dead` if it has no attempts left (for example, an event that crashes the
    process every time). Returns the recovered events and their new status.
    """
    rows = await session.scalars(
        select(Event)
        .where(Event.status == EventStatus.PROCESSING, Event.locked_until < now)
        .order_by(Event.id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    recovered: list[tuple[ClaimedEvent, EventStatus]] = []
    for row in rows.all():
        if row.claim_token is None:  # impossible: every claim sets a token
            continue
        claimed = _snapshot(row, attempts=row.attempts, claim_token=row.claim_token)
        status = await fail(
            session, claimed, "lease expired", permanent=False, now=now, policy=policy
        )
        log.warning(
            "event_lease_expired",
            event_id=claimed.id,
            user_id=str(claimed.user_id),
            attempt=claimed.attempts,
            status=status.value,
        )
        recovered.append((claimed, status))
    return recovered

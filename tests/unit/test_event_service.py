import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Database
from app.events import service
from app.events.errors import LostClaimError, PermanentEventError
from app.events.models import Event, EventSource, EventStatus, EventType
from app.events.schemas import ClaimedEvent, NewEvent
from app.events.service import EventPolicy, backoff_delay, describe_error
from app.users.models import User

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
POLICY = EventPolicy()


def _new(user: User, external_id: str, *, run_at: datetime | None = None) -> NewEvent:
    return NewEvent(
        user_id=user.id,
        type=EventType.USER_MESSAGE,
        source=EventSource.DEV,
        external_id=external_id,
        run_at=run_at,
    )


async def _enqueue(
    session: AsyncSession, user: User, external_id: str, *, run_at: datetime | None = None
) -> int:
    event_id = await service.enqueue(session, _new(user, external_id, run_at=run_at), now=NOW)
    await session.commit()
    assert event_id is not None
    return event_id


async def _claim(
    session: AsyncSession, *, now: datetime = NOW, policy: EventPolicy = POLICY
) -> ClaimedEvent | None:
    claimed = await service.claim_next(session, now=now, policy=policy)
    await session.commit()
    return claimed


async def _load(session: AsyncSession, event_id: int) -> Event:
    event = await session.get(Event, event_id, populate_existing=True)
    assert event is not None
    return event


async def _other_user(session: AsyncSession) -> User:
    other = User(telegram_user_id=2_000_000_002)
    session.add(other)
    await session.commit()
    return other


# --- enqueue ----------------------------------------------------------------------


async def test_duplicate_delivery_is_a_noop(session: AsyncSession, user: User) -> None:
    first = await service.enqueue(session, _new(user, "update-1"), now=NOW)
    duplicate = await service.enqueue(session, _new(user, "update-1"), now=NOW)
    await session.commit()

    assert first is not None
    assert duplicate is None
    count = await session.scalar(select(func.count()).select_from(Event))
    assert count == 1


async def test_same_external_id_from_another_source_is_a_different_event(
    session: AsyncSession, user: User
) -> None:
    await _enqueue(session, user, "42")
    other = NewEvent(
        user_id=user.id, type=EventType.EMAIL_RECEIVED, source=EventSource.GMAIL, external_id="42"
    )
    assert await service.enqueue(session, other, now=NOW) is not None


async def test_enqueue_for_unknown_user_still_raises(session: AsyncSession, user: User) -> None:
    stranger = NewEvent(
        user_id=uuid.uuid4(), type=EventType.USER_MESSAGE, source=EventSource.DEV, external_id="x"
    )
    with pytest.raises(IntegrityError):
        await service.enqueue(session, stranger, now=NOW)
        await session.commit()


async def test_enqueued_event_is_pending_and_due_now(session: AsyncSession, user: User) -> None:
    event = await _load(session, await _enqueue(session, user, "e"))
    assert event.status is EventStatus.PENDING
    assert event.attempts == 0
    assert event.run_at == NOW
    assert event.case_id is None


def test_new_event_rejects_naive_run_at_and_non_json_payload() -> None:
    user_id = uuid.uuid4()
    with pytest.raises(ValidationError):
        NewEvent(
            user_id=user_id,
            type=EventType.FOLLOW_UP_DUE,
            source=EventSource.SYSTEM,
            external_id="f",
            run_at=datetime(2026, 1, 1),
        )
    with pytest.raises(ValidationError):
        NewEvent(
            user_id=user_id,
            type=EventType.USER_MESSAGE,
            source=EventSource.DEV,
            external_id="p",
            payload={"bad": object()},
        )


# --- claim ------------------------------------------------------------------------


async def test_future_event_is_claimable_only_once_due(session: AsyncSession, user: User) -> None:
    due = NOW + timedelta(hours=1)
    event_id = await _enqueue(session, user, "later", run_at=due)

    assert await _claim(session) is None
    claimed = await _claim(session, now=due)
    assert claimed is not None
    assert claimed.id == event_id


async def test_one_user_runs_one_event_at_a_time_in_order(
    session: AsyncSession, user: User
) -> None:
    first = await _enqueue(session, user, "1")
    second = await _enqueue(session, user, "2")

    claimed = await _claim(session)
    assert claimed is not None
    assert (claimed.id, claimed.attempts) == (first, 1)
    event = await _load(session, first)
    assert event.status is EventStatus.PROCESSING
    assert event.locked_until == NOW + POLICY.lease

    assert await _claim(session) is None  # the user already has one processing

    await service.complete(session, claimed, now=NOW)
    await session.commit()
    done = await _load(session, first)
    assert done.status is EventStatus.DONE
    assert done.processed_at == NOW
    assert done.claim_token is None

    claimed = await _claim(session)
    assert claimed is not None
    assert claimed.id == second


async def test_another_users_event_is_not_blocked(session: AsyncSession, user: User) -> None:
    other = await _other_user(session)
    await _enqueue(session, user, "mine")
    theirs = await _enqueue(session, other, "theirs")

    assert await _claim(session) is not None
    claimed = await _claim(session)
    assert claimed is not None
    assert claimed.id == theirs


async def test_event_waiting_to_retry_holds_back_newer_events(
    session: AsyncSession, user: User
) -> None:
    first = await _enqueue(session, user, "1")
    await _enqueue(session, user, "2")

    claimed = await _claim(session)
    assert claimed is not None
    await service.fail(
        session, claimed, RuntimeError("boom"), permanent=False, now=NOW, policy=POLICY
    )
    await session.commit()

    # "2" is due, but "1" must run first so the user's messages stay in order.
    assert await _claim(session) is None
    retry = await _claim(session, now=NOW + backoff_delay(1, POLICY))
    assert retry is not None
    assert (retry.id, retry.attempts) == (first, 2)


async def test_scheduled_future_event_blocks_nothing(session: AsyncSession, user: User) -> None:
    await _enqueue(session, user, "follow-up", run_at=NOW + timedelta(days=3))
    now_event = await _enqueue(session, user, "message")

    claimed = await _claim(session)
    assert claimed is not None
    assert claimed.id == now_event


async def test_database_refuses_two_processing_events_for_one_user(
    database: Database, user: User
) -> None:
    async with database.transaction() as s:
        a = await service.enqueue(s, _new(user, "a"), now=NOW)
        b = await service.enqueue(s, _new(user, "b"), now=NOW)
    processing = {"status": EventStatus.PROCESSING, "claim_token": uuid.uuid4()}
    async with database.transaction() as s:
        await s.execute(update(Event).where(Event.id == a).values(processing))
    with pytest.raises(IntegrityError):
        async with database.transaction() as s:
            await s.execute(update(Event).where(Event.id == b).values(processing))


async def test_concurrent_claims_skip_locked_rows(database: Database, user: User) -> None:
    if database.engine.dialect.name != "postgresql":
        pytest.skip("SKIP LOCKED only exists on Postgres; SQLite runs a single worker")
    async with database.transaction() as s:
        other = User(telegram_user_id=2_000_000_002)
        s.add(other)
        await s.flush()
        a1 = await service.enqueue(s, _new(user, "a1"), now=NOW)
        await service.enqueue(s, _new(user, "a2"), now=NOW)
        b1 = await service.enqueue(s, _new(other, "b1"), now=NOW)

    async with database.transaction() as s1, database.transaction() as s2:
        first = await service.claim_next(s1, now=NOW, policy=POLICY)
        # s1 holds a1's row lock. s2 skips it, and a2 must wait behind a1.
        second = await service.claim_next(s2, now=NOW, policy=POLICY)
    assert first is not None
    assert second is not None
    assert (first.id, second.id) == (a1, b1)

    async with database.transaction() as s:
        assert await service.claim_next(s, now=NOW, policy=POLICY) is None


# --- complete / fail / release ------------------------------------------------------


async def test_complete_with_a_stale_claim_raises(session: AsyncSession, user: User) -> None:
    await _enqueue(session, user, "e")
    claimed = await _claim(session)
    assert claimed is not None
    stale = dataclasses.replace(claimed, claim_token=uuid.uuid4())

    with pytest.raises(LostClaimError):
        await service.complete(session, stale, now=NOW)


async def test_transient_failures_back_off_then_go_dead(session: AsyncSession, user: User) -> None:
    event_id = await _enqueue(session, user, "flaky")
    now = NOW
    for attempt in range(1, POLICY.max_attempts + 1):
        claimed = await _claim(session, now=now)
        assert claimed is not None
        assert claimed.attempts == attempt
        status = await service.fail(
            session, claimed, RuntimeError("down"), permanent=False, now=now, policy=POLICY
        )
        await session.commit()
        event = await _load(session, event_id)
        if attempt < POLICY.max_attempts:
            assert status is EventStatus.PENDING
            assert event.run_at == now + backoff_delay(attempt, POLICY)
            now = event.run_at
        else:
            assert status is EventStatus.DEAD
            assert event.processed_at == now
    assert event.last_error == "RuntimeError"
    assert await _claim(session, now=now + timedelta(days=1)) is None


async def test_permanent_failure_goes_dead_at_once(session: AsyncSession, user: User) -> None:
    event_id = await _enqueue(session, user, "bad")
    claimed = await _claim(session)
    assert claimed is not None

    status = await service.fail(
        session, claimed, PermanentEventError("nope"), permanent=True, now=NOW, policy=POLICY
    )
    await session.commit()

    assert status is EventStatus.DEAD
    event = await _load(session, event_id)
    assert event.status is EventStatus.DEAD
    assert event.last_error == "PermanentEventError: nope"


async def test_release_hands_back_without_counting_the_attempt(
    session: AsyncSession, user: User
) -> None:
    event_id = await _enqueue(session, user, "e")
    claimed = await _claim(session)
    assert claimed is not None

    await service.release(session, claimed, now=NOW)
    await session.commit()

    event = await _load(session, event_id)
    assert (event.status, event.attempts, event.claim_token) == (EventStatus.PENDING, 0, None)
    assert await _claim(session) is not None


# --- lease recovery ------------------------------------------------------------------


async def test_expired_lease_is_recovered_as_a_failed_attempt(
    session: AsyncSession, user: User
) -> None:
    event_id = await _enqueue(session, user, "crashed")
    claimed = await _claim(session)
    assert claimed is not None

    assert await service.recover_expired(session, now=NOW + POLICY.lease, policy=POLICY) == []

    later = NOW + POLICY.lease + timedelta(seconds=1)
    recovered = await service.recover_expired(session, now=later, policy=POLICY)
    await session.commit()

    assert [(e.id, status) for e, status in recovered] == [(event_id, EventStatus.PENDING)]
    event = await _load(session, event_id)
    assert event.status is EventStatus.PENDING
    assert event.attempts == 1
    assert event.last_error == "lease expired"
    assert event.run_at == later + backoff_delay(1, POLICY)
    # The crashed worker's claim is no longer valid.
    with pytest.raises(LostClaimError):
        await service.complete(session, claimed, now=later)


async def test_event_that_keeps_crashing_goes_dead(session: AsyncSession, user: User) -> None:
    policy = EventPolicy(max_attempts=1)
    event_id = await _enqueue(session, user, "poison")
    assert await _claim(session, policy=policy) is not None

    later = NOW + policy.lease + timedelta(seconds=1)
    recovered = await service.recover_expired(session, now=later, policy=policy)
    await session.commit()

    assert [status for _, status in recovered] == [EventStatus.DEAD]
    assert (await _load(session, event_id)).status is EventStatus.DEAD


# --- pure helpers ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "seconds"),
    [(1, 15), (2, 30), (3, 60), (4, 120), (5, 240), (6, 480), (7, 960), (8, 1800), (1000, 1800)],
)
def test_backoff_delay(attempt: int, seconds: int) -> None:
    assert backoff_delay(attempt, POLICY) == timedelta(seconds=seconds)


def test_default_policy_gives_up_after_about_thirty_minutes() -> None:
    total = sum((backoff_delay(a, POLICY) for a in range(1, POLICY.max_attempts)), timedelta())
    assert timedelta(minutes=25) < total < timedelta(minutes=40)


def test_backoff_delay_is_one_based() -> None:
    with pytest.raises(ValueError, match="1-based"):
        backoff_delay(0, POLICY)


def test_describe_error_keeps_only_the_type_of_third_party_errors() -> None:
    leaky = ValueError("GET https://api.telegram.org/bot123:SECRET/sendMessage failed")
    assert describe_error(leaky) == "ValueError"
    assert describe_error(LostClaimError(7)) == (
        "LostClaimError: event 7 is no longer claimed by this worker"
    )

"""The event worker end to end against a real database (SQLite, and Postgres in CI)."""

import asyncio
import shutil
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import func, select

from app.cases import service as case_service
from app.cases.models import SupportCase, TransitionActor
from app.config import Environment, Settings
from app.db.base import utcnow
from app.db.session import Database
from app.events import service
from app.events.handlers import HandlerContext, HandlerRegistry, UnparsedPayload
from app.events.models import Event, EventSource, EventStatus, EventType
from app.events.schemas import ClaimedEvent, NewEvent
from app.events.service import EventPolicy, backoff_delay
from app.events.worker import EventWorker
from app.main import create_app
from app.users.models import User
from tests.conftest import sqlite_url

START = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class RecordingNotifier:
    def __init__(self) -> None:
        self.dead: list[tuple[ClaimedEvent, str]] = []

    async def event_dead(self, event: ClaimedEvent, error_type: str) -> None:
        self.dead.append((event, error_type))


type TestHandler = Callable[[HandlerContext, UnparsedPayload], Awaitable[None]]


def _registry(handler: TestHandler) -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register(EventType.USER_MESSAGE, UnparsedPayload, handler)
    return registry


def _worker(
    database: Database,
    registry: HandlerRegistry,
    *,
    clock: Callable[[], datetime] | None = None,
    notifier: RecordingNotifier | None = None,
    policy: EventPolicy | None = None,
    concurrency: int = 4,
) -> EventWorker:
    return EventWorker(
        database,
        registry,
        policy or EventPolicy(),
        notifier=notifier or RecordingNotifier(),
        concurrency=concurrency,
        poll_interval=0.01,
        clock=clock or utcnow,
    )


async def _enqueue(
    database: Database, user: User, external_id: str, *, now: datetime | None = None
) -> int:
    new_event = NewEvent(
        user_id=user.id,
        type=EventType.USER_MESSAGE,
        source=EventSource.DEV,
        external_id=external_id,
    )
    async with database.transaction() as s:
        event_id = await service.enqueue(s, new_event, now=now)
    assert event_id is not None
    return event_id


async def _event(database: Database, event_id: int) -> Event:
    async with database.session_factory() as s:
        event = await s.get(Event, event_id)
        assert event is not None
        return event


async def _count(database: Database, model: type[SupportCase] | type[Event]) -> int:
    async with database.session_factory() as s:
        return await s.scalar(select(func.count()).select_from(model)) or 0


async def _wait_until(predicate: Callable[[], Awaitable[bool]]) -> None:
    """Poll the database until `predicate` holds, for up to 10 seconds."""
    for _ in range(500):
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached within 10 seconds")


async def _open_case(ctx: HandlerContext) -> None:
    await case_service.create_case(
        ctx.session, user_id=ctx.event.user_id, actor=TransitionActor.SYSTEM, event_id=ctx.event.id
    )


# --- Outcomes ------------------------------------------------------------------------


async def test_success_commits_handler_writes_with_the_event(
    database: Database, user: User
) -> None:
    async def handler(ctx: HandlerContext, payload: UnparsedPayload) -> None:
        await _open_case(ctx)
        # Follow-on events are enqueued in the same transaction.
        follow_up = NewEvent(
            user_id=ctx.event.user_id,
            type=EventType.FOLLOW_UP_DUE,
            source=EventSource.SYSTEM,
            external_id=f"follow-up:{ctx.event.id}",
            run_at=ctx.now + timedelta(days=3),
        )
        await service.enqueue(ctx.session, follow_up, now=ctx.now)

    clock = FakeClock()
    event_id = await _enqueue(database, user, "hello", now=clock())

    processed = await _worker(database, _registry(handler), clock=clock).run_until_idle()

    assert processed == 1
    event = await _event(database, event_id)
    assert event.status is EventStatus.DONE
    assert event.attempts == 1
    assert await _count(database, SupportCase) == 1
    assert await _count(database, Event) == 2


async def test_failures_roll_back_retry_with_backoff_then_notify_once(
    database: Database, user: User
) -> None:
    calls = 0

    async def handler(ctx: HandlerContext, payload: UnparsedPayload) -> None:
        nonlocal calls
        calls += 1
        await _open_case(ctx)
        raise RuntimeError("OpenAI is down")

    clock = FakeClock()
    notifier = RecordingNotifier()
    policy = EventPolicy()
    worker = _worker(database, _registry(handler), clock=clock, notifier=notifier, policy=policy)
    event_id = await _enqueue(database, user, "hello", now=clock())

    for attempt in range(1, policy.max_attempts + 1):
        assert await worker.run_until_idle() == 1
        # Not due again until the backoff has passed.
        assert await worker.run_until_idle() == 0
        clock.advance(backoff_delay(attempt, policy))

    assert calls == policy.max_attempts
    event = await _event(database, event_id)
    assert event.status is EventStatus.DEAD
    assert event.last_error == "RuntimeError"
    assert await _count(database, SupportCase) == 0  # every attempt rolled back
    assert [(e.id, error_type) for e, error_type in notifier.dead] == [(event_id, "RuntimeError")]


async def test_invalid_payload_goes_dead_without_retrying(database: Database, user: User) -> None:
    class NeedsText(BaseModel):
        text: str

    async def handler(ctx: HandlerContext, payload: NeedsText) -> None:
        raise AssertionError("must not run")

    registry = HandlerRegistry()
    registry.register(EventType.USER_MESSAGE, NeedsText, handler)
    notifier = RecordingNotifier()
    event_id = await _enqueue(database, user, "no-text")

    await _worker(database, registry, notifier=notifier).run_until_idle()

    assert (await _event(database, event_id)).status is EventStatus.DEAD
    assert [error_type for _, error_type in notifier.dead] == ["InvalidEventPayloadError"]


async def test_event_without_a_handler_goes_dead(database: Database, user: User) -> None:
    notifier = RecordingNotifier()
    event_id = await _enqueue(database, user, "orphan")

    await _worker(database, HandlerRegistry(), notifier=notifier).run_until_idle()

    assert (await _event(database, event_id)).status is EventStatus.DEAD
    assert [error_type for _, error_type in notifier.dead] == ["UnknownEventTypeError"]


async def test_handler_timeout_is_retried(database: Database, user: User) -> None:
    async def handler(ctx: HandlerContext, payload: UnparsedPayload) -> None:
        await asyncio.sleep(5)

    policy = EventPolicy(lease=timedelta(seconds=0.2))
    event_id = await _enqueue(database, user, "slow")

    await _worker(database, _registry(handler), policy=policy).run_until_idle()

    event = await _event(database, event_id)
    assert (event.status, event.attempts, event.last_error) == (
        EventStatus.PENDING,
        1,
        "TimeoutError",
    )


async def test_lost_claim_rolls_back_the_handlers_work(database: Database, user: User) -> None:
    async def handler(ctx: HandlerContext, payload: UnparsedPayload) -> None:
        # Simulate the lease expiring and another worker recovering the event.
        async with database.transaction() as other:
            later = ctx.now + timedelta(hours=1)
            await service.recover_expired(other, now=later, policy=EventPolicy())
        await _open_case(ctx)

    notifier = RecordingNotifier()
    event_id = await _enqueue(database, user, "e")

    await _worker(database, _registry(handler), notifier=notifier).run_until_idle()

    event = await _event(database, event_id)
    assert (event.status, event.last_error) == (EventStatus.PENDING, "lease expired")
    assert await _count(database, SupportCase) == 0
    assert notifier.dead == []


# --- Background loop -------------------------------------------------------------------


async def test_a_users_events_never_overlap_and_run_in_order(
    database: Database, user: User
) -> None:
    in_flight: dict[str, int] = defaultdict(int)
    max_in_flight: dict[str, int] = defaultdict(int)
    max_total = 0
    order: dict[str, list[int]] = defaultdict(list)

    async def handler(ctx: HandlerContext, payload: UnparsedPayload) -> None:
        nonlocal max_total
        key = str(ctx.event.user_id)
        in_flight[key] += 1
        max_in_flight[key] = max(max_in_flight[key], in_flight[key])
        max_total = max(max_total, sum(in_flight.values()))
        order[key].append(ctx.event.id)
        await asyncio.sleep(0.05)
        in_flight[key] -= 1

    async with database.transaction() as s:
        other = User(telegram_user_id=2_000_000_002)
        s.add(other)
    ids: dict[str, list[int]] = {str(user.id): [], str(other.id): []}
    for i in range(4):
        for u in (user, other):
            ids[str(u.id)].append(await _enqueue(database, u, f"{u.telegram_user_id}-{i}"))

    worker = _worker(database, _registry(handler), concurrency=4)
    worker.start()

    async def all_done() -> bool:
        async with database.session_factory() as s:
            pending = await s.scalar(
                select(func.count()).select_from(Event).where(Event.status != EventStatus.DONE)
            )
            return pending == 0

    try:
        await _wait_until(all_done)
    finally:
        await worker.stop()

    assert set(max_in_flight.values()) == {1}
    assert dict(order) == ids
    if worker.concurrency > 1:
        # Different users do run side by side.
        assert max_total == 2


async def test_stop_releases_in_flight_events(database: Database, user: User) -> None:
    started = asyncio.Event()

    async def handler(ctx: HandlerContext, payload: UnparsedPayload) -> None:
        started.set()
        await asyncio.Event().wait()  # never finishes on its own

    event_id = await _enqueue(database, user, "stuck")
    worker = _worker(database, _registry(handler))
    worker.start()
    async with asyncio.timeout(10):
        await started.wait()

    await worker.stop(grace=0.1)

    event = await _event(database, event_id)
    assert (event.status, event.attempts, event.claim_token) == (EventStatus.PENDING, 0, None)


async def test_app_lifespan_runs_the_worker(tmp_path: Path, sqlite_template: Path) -> None:
    db_path = tmp_path / "app.db"
    shutil.copyfile(sqlite_template, db_path)
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        database_url=sqlite_url(db_path),
        worker_poll_interval_seconds=0.01,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        database: Database = app.state.database
        async with database.transaction() as s:
            user = User(telegram_user_id=3_000_000_003)
            s.add(user)
        event_id = await _enqueue(database, user, "via-app")
        app.state.worker.wake()

        async def done() -> bool:
            return (await _event(database, event_id)).status is EventStatus.DONE

        await _wait_until(done)

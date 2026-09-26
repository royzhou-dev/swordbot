"""The in-process event worker (PLAN D1).

Started from the app lifespan. It repeatedly recovers expired leases, claims
runnable events, and runs each one in its own task, up to `concurrency` at a
time. Serialization per user is enforced by the claim query and the database,
not by this loop, so several worker processes are safe too.

Each event runs in one transaction: handler work plus `complete()`. On failure
that transaction rolls back and a separate one records the failure, which
reschedules the event with backoff or marks it `dead` and notifies the user.
"""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import structlog
from sqlalchemy.exc import IntegrityError

from app.db.base import utcnow
from app.db.session import Database
from app.events import service
from app.events.errors import LostClaimError, PermanentEventError
from app.events.handlers import HandlerContext, HandlerRegistry
from app.events.models import EventStatus
from app.events.schemas import ClaimedEvent
from app.events.service import EventPolicy
from app.logging import get_logger

log = get_logger(__name__)

# How long the loop waits after an unexpected error (e.g. the database is down).
_ERROR_BACKOFF_SECONDS = 5.0


class DeadEventNotifier(Protocol):
    """Told about every event that will not be retried, so the user can be informed."""

    async def event_dead(self, event: ClaimedEvent, error_type: str) -> None: ...


class LoggingDeadEventNotifier:
    """Logs dead events. M3 replaces it with a Telegram notification."""

    async def event_dead(self, event: ClaimedEvent, error_type: str) -> None:
        log.error(
            "event_dead",
            event_id=event.id,
            user_id=str(event.user_id),
            event_type=event.type.value,
            attempts=event.attempts,
            error_type=error_type,
        )


class EventWorker:
    def __init__(
        self,
        database: Database,
        registry: HandlerRegistry,
        policy: EventPolicy,
        *,
        notifier: DeadEventNotifier,
        concurrency: int = 4,
        poll_interval: float = 1.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._database = database
        self._registry = registry
        self._policy = policy
        self._notifier = notifier
        # SQLite allows one writer at a time, so a single in-flight event.
        self._concurrency = 1 if database.engine.dialect.name == "sqlite" else concurrency
        self._poll_interval = poll_interval
        self._clock = clock
        self._wake = asyncio.Event()
        self._stopping = False
        self._loop_task: asyncio.Task[None] | None = None
        self._in_flight: dict[asyncio.Task[None], ClaimedEvent] = {}

    @property
    def concurrency(self) -> int:
        return self._concurrency

    # --- Lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._loop_task is not None:
            raise RuntimeError("worker already started")
        self._stopping = False
        self._loop_task = asyncio.create_task(self._run(), name="event-worker")
        log.info("worker_started", concurrency=self._concurrency)

    def wake(self) -> None:
        """Check for work now instead of at the next poll (call after enqueueing)."""
        self._wake.set()

    async def stop(self, grace: float = 10.0) -> None:
        """Stop claiming, give in-flight events `grace` seconds, then cancel and release them."""
        self._stopping = True
        self._wake.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        if self._in_flight:
            _, pending = await asyncio.wait(set(self._in_flight), timeout=grace)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        log.info("worker_stopped")

    async def run_until_idle(self) -> int:
        """Process events one at a time until none is runnable. Returns how many ran.

        For tests and scripts; the app uses `start()`.
        """
        processed = 0
        while True:
            await self._recover_expired()
            claimed = await self._claim()
            if claimed is None:
                return processed
            await self._process(claimed)
            processed += 1

    # --- Loop ------------------------------------------------------------------

    async def _run(self) -> None:
        while not self._stopping:
            self._wake.clear()
            try:
                await self._fill_slots()
            except Exception as exc:
                log.error("worker_loop_error", error_type=type(exc).__name__)
                await asyncio.sleep(_ERROR_BACKOFF_SECONDS)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)

    async def _fill_slots(self) -> None:
        if len(self._in_flight) >= self._concurrency:
            return
        await self._recover_expired()
        while len(self._in_flight) < self._concurrency and not self._stopping:
            claimed = await self._claim()
            if claimed is None:
                return
            task = asyncio.create_task(self._process(claimed), name=f"event-{claimed.id}")
            self._in_flight[task] = claimed
            task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._in_flight.pop(task, None)
        # A finished event may unblock the same user's next event.
        self._wake.set()

    async def _claim(self) -> ClaimedEvent | None:
        try:
            async with self._database.transaction() as session:
                return await service.claim_next(session, now=self._clock(), policy=self._policy)
        except IntegrityError:
            # Another worker claimed an event for the same user first.
            return None

    async def _recover_expired(self) -> None:
        async with self._database.transaction() as session:
            recovered = await service.recover_expired(
                session, now=self._clock(), policy=self._policy
            )
        for event, status in recovered:
            if status is EventStatus.DEAD:
                await self._notify_dead(event, "LeaseExpired")

    # --- One event -------------------------------------------------------------

    async def _process(self, event: ClaimedEvent) -> None:
        with structlog.contextvars.bound_contextvars(
            event_id=event.id,
            user_id=str(event.user_id),
            case_id=str(event.case_id) if event.case_id else None,
            event_type=event.type.value,
        ):
            event_log = log.bind()
            event_log.info("event_claimed", attempt=event.attempts)
            try:
                async with asyncio.timeout(self._policy.handler_timeout.total_seconds()):
                    async with self._database.transaction() as session:
                        ctx = HandlerContext(
                            session=session, event=event, log=event_log, now=self._clock()
                        )
                        await self._registry.dispatch(ctx)
                        await service.complete(session, event, now=self._clock())
            except asyncio.CancelledError:
                if self._stopping:
                    await asyncio.shield(self._release(event))
                raise
            except LostClaimError:
                # The lease expired and the event was recovered; our work was rolled back.
                event_log.warning("event_claim_lost")
                return
            except Exception as exc:
                await self._fail(event, exc, event_log)
                return
            event_log.info("event_done")

    async def _fail(
        self, event: ClaimedEvent, exc: Exception, event_log: structlog.stdlib.BoundLogger
    ) -> None:
        # Only the error type is logged: messages can contain user content or tokens.
        error_type = type(exc).__name__
        permanent = isinstance(exc, PermanentEventError)
        try:
            async with self._database.transaction() as session:
                status = await service.fail(
                    session, event, exc, permanent=permanent, now=self._clock(), policy=self._policy
                )
        except LostClaimError:
            event_log.warning("event_claim_lost")
            return
        except Exception as record_exc:
            # The lease will expire and recovery will count the attempt.
            event_log.error(
                "event_fail_not_recorded",
                error_type=error_type,
                record_error_type=type(record_exc).__name__,
            )
            return
        event_log.warning(
            "event_failed",
            error_type=error_type,
            attempt=event.attempts,
            permanent=permanent,
            status=status.value,
        )
        if status is EventStatus.DEAD:
            await self._notify_dead(event, error_type)

    async def _release(self, event: ClaimedEvent) -> None:
        try:
            async with self._database.transaction() as session:
                await service.release(session, event, now=self._clock())
            log.info("event_released", event_id=event.id)
        except Exception as exc:
            log.error("event_release_failed", event_id=event.id, error_type=type(exc).__name__)

    async def _notify_dead(self, event: ClaimedEvent, error_type: str) -> None:
        try:
            await self._notifier.event_dead(event, error_type)
        except Exception as exc:
            log.error("event_dead_notify_failed", event_id=event.id, error_type=type(exc).__name__)

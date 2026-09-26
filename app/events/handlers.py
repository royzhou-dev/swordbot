"""Event handlers and the registry that dispatches to them.

A handler receives a `HandlerContext` and its event's payload, already
validated into the Pydantic model it registered. It runs inside the same
transaction that marks the event done, so its database writes (including any
follow-on events it enqueues) commit or roll back together with the event.

Handlers may be re-run after a crash (at-least-once delivery), so any external
action they take must be guarded against duplicates (PLAN D2).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.errors import InvalidEventPayloadError, UnknownEventTypeError
from app.events.models import EventType
from app.events.schemas import ClaimedEvent


@dataclass(frozen=True, slots=True)
class HandlerContext:
    session: AsyncSession
    event: ClaimedEvent
    # Already bound to the event's ids.
    log: structlog.stdlib.BoundLogger
    now: datetime


type Handler[P: BaseModel] = Callable[[HandlerContext, P], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Route:
    payload_model: type[BaseModel]
    handler: Callable[[HandlerContext, Any], Awaitable[None]]


class HandlerRegistry:
    """Maps each event type to one handler and its payload model."""

    def __init__(self) -> None:
        self._routes: dict[EventType, _Route] = {}

    def register[P: BaseModel](
        self, event_type: EventType, payload_model: type[P], handler: Handler[P]
    ) -> None:
        if event_type in self._routes:
            raise ValueError(f"a handler for {event_type.value} is already registered")
        self._routes[event_type] = _Route(payload_model, handler)

    def handles(self, event_type: EventType) -> bool:
        return event_type in self._routes

    async def dispatch(self, ctx: HandlerContext) -> None:
        """Validate the payload and run the handler.

        Raises `UnknownEventTypeError` or `InvalidEventPayloadError`, both
        permanent, when the event can never be handled.
        """
        event_type = ctx.event.type
        route = self._routes.get(event_type)
        if route is None:
            raise UnknownEventTypeError(event_type)
        try:
            payload = route.payload_model.model_validate(ctx.event.payload)
        except ValidationError as exc:
            locations = [".".join(str(part) for part in err["loc"]) for err in exc.errors()]
            raise InvalidEventPayloadError(event_type, locations) from None
        await route.handler(ctx, payload)


# --- Handlers -------------------------------------------------------------------


class UnparsedPayload(BaseModel):
    """Accepts any payload. Used by placeholder handlers until the real schema exists."""

    model_config = ConfigDict(extra="allow")


async def log_user_message(ctx: HandlerContext, payload: UnparsedPayload) -> None:
    """Placeholder until M3 replaces it with the Telegram echo handler."""
    # Only ids are logged, never message content.
    ctx.log.info("user_message_received")


def build_registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register(EventType.USER_MESSAGE, UnparsedPayload, log_user_message)
    return registry

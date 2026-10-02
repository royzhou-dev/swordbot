import uuid
from datetime import UTC, datetime
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Database
from app.events.errors import InvalidEventPayloadError, UnknownEventTypeError
from app.events.handlers import HandlerContext, HandlerRegistry
from app.events.models import EventSource, EventType
from app.events.routing import build_registry
from app.events.schemas import ClaimedEvent
from app.logging import get_logger
from tests.fakes import FakeGmailClient, FakeLLMClient, FakeTelegramClient


class Greeting(BaseModel):
    name: str
    times: int = 1


def _ctx(event_type: EventType, payload: dict[str, object]) -> HandlerContext:
    event = ClaimedEvent(
        id=1,
        user_id=uuid.uuid4(),
        case_id=None,
        type=event_type,
        source=EventSource.DEV,
        external_id="x",
        payload=payload,
        attempts=1,
        claim_token=uuid.uuid4(),
        created_at=datetime.now(UTC),
    )
    # These handlers never touch the database.
    return HandlerContext(
        session=cast(AsyncSession, None), event=event, log=get_logger(), now=datetime.now(UTC)
    )


async def test_dispatch_passes_the_validated_payload() -> None:
    received: list[Greeting] = []

    async def handler(ctx: HandlerContext, payload: Greeting) -> None:
        received.append(payload)

    registry = HandlerRegistry()
    registry.register(EventType.USER_MESSAGE, Greeting, handler)
    await registry.dispatch(_ctx(EventType.USER_MESSAGE, {"name": "Roy", "times": 2}))

    assert received == [Greeting(name="Roy", times=2)]


async def test_invalid_payload_is_permanent_and_does_not_echo_values() -> None:
    async def handler(ctx: HandlerContext, payload: Greeting) -> None:
        raise AssertionError("must not run")

    registry = HandlerRegistry()
    registry.register(EventType.USER_MESSAGE, Greeting, handler)

    with pytest.raises(InvalidEventPayloadError) as info:
        await registry.dispatch(_ctx(EventType.USER_MESSAGE, {"times": "my card is 4111"}))
    assert "4111" not in str(info.value)
    assert "name" in str(info.value)


async def test_unregistered_type_is_permanent() -> None:
    with pytest.raises(UnknownEventTypeError):
        await HandlerRegistry().dispatch(_ctx(EventType.EMAIL_RECEIVED, {}))


def test_registering_a_type_twice_is_an_error() -> None:
    async def handler(ctx: HandlerContext, payload: Greeting) -> None:
        return None

    registry = HandlerRegistry()
    registry.register(EventType.USER_MESSAGE, Greeting, handler)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(EventType.USER_MESSAGE, Greeting, handler)


def test_default_registry_handles_chat_events() -> None:
    registry = build_registry(
        FakeTelegramClient(),
        FakeLLMClient(),
        gmail=FakeGmailClient(),
        database=cast(Database, None),
        user_timezone=ZoneInfo("UTC"),
    )
    for event_type in (
        EventType.USER_MESSAGE,
        EventType.USER_BUTTON_ACTION,
        EventType.DRAFT_EMAIL,
        EventType.SEND_EMAIL,
        EventType.SEARCH_RECEIPTS,
        EventType.TELEGRAM_OUTBOUND,
    ):
        assert registry.handles(event_type)

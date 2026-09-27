"""Normalizing and authorizing raw Telegram updates into queued events."""

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Database
from app.events.models import Event, EventSource, EventType
from app.telegram.ingest import ingest_update
from app.telegram.keyboards import encode_callback_data
from app.users.models import User
from tests.fakes import callback_update, message_update

OWNER = 1_000_000_001
STRANGER = 2_000_000_002


async def _ingest(
    database: Database, raw: dict[str, Any], allowed: int | None = OWNER
) -> int | None:
    async with database.transaction() as session:
        return await ingest_update(session, raw, allowed_user_id=allowed)


async def _events(session: AsyncSession) -> list[Event]:
    return list((await session.scalars(select(Event).order_by(Event.id))).all())


async def _user_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(User)) or 0


async def test_owner_message_becomes_a_user_message_event(
    database: Database, session: AsyncSession
) -> None:
    event_id = await _ingest(database, message_update(10, sender_id=OWNER, message_id=3))

    assert event_id is not None
    [event] = await _events(session)
    assert event.type is EventType.USER_MESSAGE
    assert event.source is EventSource.TELEGRAM
    assert event.external_id == f"message:{OWNER}:3"
    assert event.payload == {"text": "hello", "telegram_message_id": 3}
    user = await session.get(User, event.user_id)
    assert user is not None and user.telegram_user_id == OWNER


async def test_redelivered_update_is_stored_once(database: Database, session: AsyncSession) -> None:
    raw = message_update(10, sender_id=OWNER, message_id=3)
    assert await _ingest(database, raw) is not None
    assert await _ingest(database, raw) is None
    assert len(await _events(session)) == 1


async def test_dedup_key_is_the_message_not_the_update_id(
    database: Database, session: AsyncSession
) -> None:
    # Telegram can reuse update_id values after a week of silence.
    assert await _ingest(database, message_update(10, sender_id=OWNER, message_id=3)) is not None
    assert await _ingest(database, message_update(10, sender_id=OWNER, message_id=4)) is not None
    assert len(await _events(session)) == 2


@pytest.mark.parametrize(
    ("raw", "allowed"),
    [
        (message_update(1, sender_id=STRANGER), OWNER),
        (message_update(1, sender_id=OWNER), None),
        (message_update(1, sender_id=OWNER, chat_type="group"), OWNER),
        (message_update(1, sender_id=OWNER, is_bot=True), OWNER),
        (callback_update(1, sender_id=STRANGER, callback_id="q", data="a:00"), OWNER),
    ],
    ids=["stranger", "no-allowed-user", "group-chat", "bot-sender", "stranger-button"],
)
async def test_unauthorized_updates_are_dropped_without_creating_anything(
    database: Database, session: AsyncSession, raw: dict[str, Any], allowed: int | None
) -> None:
    assert await _ingest(database, raw, allowed) is None
    assert await _events(session) == []
    assert await _user_count(session) == 0


@pytest.mark.parametrize(
    "raw",
    [
        {"update_id": 1, "edited_message": {"message_id": 1}},
        {"update_id": 1, "my_chat_member": {}},
        {"update_id": "x"},
        {"message": {"message_id": 1}},
    ],
    ids=["edited", "chat-member", "bad-id", "no-update-id"],
)
async def test_unsupported_or_malformed_updates_are_dropped(
    database: Database, session: AsyncSession, raw: dict[str, Any]
) -> None:
    assert await _ingest(database, raw) is None
    assert await _events(session) == []


async def test_non_text_message_is_queued_with_no_text(
    database: Database, session: AsyncSession
) -> None:
    await _ingest(database, message_update(1, sender_id=OWNER, text=None))
    [event] = await _events(session)
    assert event.payload["text"] is None


async def test_button_press_becomes_a_button_event_with_the_action_id(
    database: Database, session: AsyncSession
) -> None:
    action_id = uuid.uuid4()
    raw = callback_update(
        5, sender_id=OWNER, callback_id="cb-1", data=encode_callback_data(action_id), message_id=9
    )

    assert await _ingest(database, raw) is not None
    assert await _ingest(database, raw) is None

    [event] = await _events(session)
    assert event.type is EventType.USER_BUTTON_ACTION
    assert event.external_id == "callback:cb-1"
    assert event.payload == {
        "action_id": str(action_id),
        "callback_query_id": "cb-1",
        "telegram_message_id": 9,
    }


async def test_button_press_with_foreign_data_is_still_queued_for_a_reply(
    database: Database, session: AsyncSession
) -> None:
    # The handler must still answer the press, so it isn't dropped here.
    await _ingest(database, callback_update(5, sender_id=OWNER, callback_id="q", data="junk"))
    [event] = await _events(session)
    assert event.payload["action_id"] is None


async def test_the_users_telegram_name_is_kept_current(
    database: Database, session: AsyncSession
) -> None:
    # It signs outbound emails by default.
    await _ingest(database, message_update(1, sender_id=OWNER, message_id=1))
    user = (await session.scalars(select(User))).one()
    assert user.display_name == "Test User"

    renamed = message_update(2, sender_id=OWNER, message_id=2)
    renamed["message"]["from"] = {"id": OWNER, "is_bot": False, "first_name": "Roy"}
    await _ingest(database, renamed)
    await session.refresh(user)
    assert user.display_name == "Roy"

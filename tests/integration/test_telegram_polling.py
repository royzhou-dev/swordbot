"""Local long polling feeds the same ingest path as the webhook."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Database
from app.events.models import Event
from app.telegram.errors import TelegramAuthenticationError
from app.telegram.ingest import ALLOWED_UPDATES
from app.telegram.polling import WebhookActiveError, poll_once, run_polling
from tests.fakes import FakeTelegramClient, message_update

OWNER = 1_000_000_001


async def _event_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Event)) or 0


async def test_poll_stores_updates_and_advances_the_offset(
    database: Database, session: AsyncSession
) -> None:
    telegram = FakeTelegramClient()
    telegram.update_batches = [
        [
            message_update(10, sender_id=OWNER, message_id=1),
            message_update(11, sender_id=999, message_id=1),  # stranger: dropped
            message_update(12, sender_id=OWNER, message_id=2),
        ]
    ]

    offset = await poll_once(telegram, database, offset=None, allowed_user_id=OWNER)

    assert offset == 13
    assert await _event_count(session) == 2
    [call] = telegram.calls_to("get_updates")
    assert call["offset"] is None
    assert call["allowed_updates"] == ALLOWED_UPDATES


async def test_refetched_updates_are_not_stored_twice(
    database: Database, session: AsyncSession
) -> None:
    telegram = FakeTelegramClient()
    batch = [message_update(10, sender_id=OWNER)]
    telegram.update_batches = [batch, batch]

    await poll_once(telegram, database, offset=None, allowed_user_id=OWNER)
    await poll_once(telegram, database, offset=None, allowed_user_id=OWNER)

    assert await _event_count(session) == 1


async def test_empty_poll_keeps_the_offset(database: Database) -> None:
    offset = await poll_once(FakeTelegramClient(), database, offset=7, allowed_user_id=OWNER)
    assert offset == 7


async def test_polling_stops_on_a_bad_token(database: Database) -> None:
    telegram = FakeTelegramClient()
    telegram.fail("get_updates", TelegramAuthenticationError("401"))

    with pytest.raises(TelegramAuthenticationError):
        await run_polling(telegram, database, allowed_user_id=OWNER)

    assert [c.method for c in telegram.calls] == ["get_webhook_info"]


async def test_polling_refuses_to_remove_a_webhook(database: Database) -> None:
    # The deployed app's webhook: polling would divert its messages to this database.
    telegram = FakeTelegramClient()
    telegram.webhook_url = "https://swordbot.example/telegram/webhook"

    with pytest.raises(WebhookActiveError):
        await run_polling(telegram, database, allowed_user_id=OWNER)

    assert telegram.calls_to("delete_webhook") == []
    assert telegram.calls_to("get_updates") == []


async def test_polling_takes_over_a_webhook_when_told_to(database: Database) -> None:
    telegram = FakeTelegramClient()
    telegram.webhook_url = "https://swordbot.example/telegram/webhook"
    telegram.fail("get_updates", TelegramAuthenticationError("401"))

    with pytest.raises(TelegramAuthenticationError):
        await run_polling(telegram, database, allowed_user_id=OWNER, take_over=True)

    # Pending updates are kept, so nothing sent meanwhile is lost.
    assert telegram.calls_to("delete_webhook") == [{"drop_pending_updates": False}]

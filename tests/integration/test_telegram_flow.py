"""Telegram end to end with a fake Bot API.

A raw update becomes an event, its handler queues replies, and the worker delivers them.

Runs against a real database (SQLite, and Postgres in CI) and the real worker.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.models import ActionStatus, PendingAction
from app.chat.handlers import NOT_TEXT_REPLY, STALE_BUTTON_REPLY
from app.db.base import utcnow
from app.db.session import Database
from app.events.models import Event, EventStatus, EventType
from app.events.routing import build_registry
from app.events.service import EventPolicy
from app.events.worker import EventWorker
from app.telegram.delivery import MAX_MESSAGE_LENGTH
from app.telegram.errors import TelegramRequestError, TelegramTemporaryError
from app.telegram.ingest import ingest_update
from app.telegram.keyboards import encode_callback_data
from app.telegram.notifier import TelegramDeadEventNotifier
from app.users.models import User
from tests.fakes import FakeTelegramClient, callback_update, message_update

OWNER = 1_000_000_001  # the `user` fixture's Telegram id


class Clock:
    """Real time plus an offset. Events are stamped with real time when ingested,
    so a frozen clock would never see them as due.
    """

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def __call__(self) -> datetime:
        return utcnow() + self.offset

    def advance(self, delta: timedelta) -> None:
        self.offset += delta


def _worker(database: Database, telegram: FakeTelegramClient, clock: Clock) -> EventWorker:
    return EventWorker(
        database,
        build_registry(telegram),
        EventPolicy(),
        notifier=TelegramDeadEventNotifier(database),
        poll_interval=0.01,
        clock=clock,
    )


async def _ingest(database: Database, raw: dict[str, Any]) -> int | None:
    async with database.transaction() as session:
        return await ingest_update(session, raw, allowed_user_id=OWNER)


def _button_data(send_call: dict[str, Any]) -> str:
    markup = send_call["reply_markup"]
    [[button]] = markup["inline_keyboard"]
    data: str = button["callback_data"]
    return data


async def _statuses(session: AsyncSession) -> list[tuple[EventType, EventStatus]]:
    session.expire_all()
    rows = await session.scalars(select(Event).order_by(Event.id))
    return [(e.type, e.status) for e in rows.all()]


async def test_message_is_echoed_with_a_button_that_works_once(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    clock = Clock()
    worker = _worker(database, telegram, clock)

    await _ingest(database, message_update(1, sender_id=OWNER, text="my fries are missing"))
    await worker.run_until_idle()

    [send] = telegram.calls_to("send_message")
    assert send["chat_id"] == OWNER
    assert send["text"] == "You said: my fries are missing"
    data = _button_data(send)
    [action] = (await session.scalars(select(PendingAction))).all()
    assert data == encode_callback_data(action.id)
    assert action.telegram_message_id is not None

    # Press it.
    await _ingest(
        database,
        callback_update(
            2, sender_id=OWNER, callback_id="cb-1", data=data, message_id=action.telegram_message_id
        ),
    )
    await worker.run_until_idle()

    assert telegram.calls_to("edit_message_reply_markup") == [
        {
            "chat_id": OWNER,
            "message_id": action.telegram_message_id,
            "reply_markup": {"inline_keyboard": []},
        }
    ]
    assert telegram.calls_to("answer_callback_query") == [
        {"callback_query_id": "cb-1", "text": None}
    ]
    assert telegram.sent_texts[-1] == 'You pressed "Test button". Buttons work.'
    await session.refresh(action)
    assert action.status is ActionStatus.CONSUMED

    # Press it again (a new callback query): rejected, nothing else happens.
    await _ingest(database, callback_update(3, sender_id=OWNER, callback_id="cb-2", data=data))
    await worker.run_until_idle()

    assert telegram.calls_to("answer_callback_query")[-1] == {
        "callback_query_id": "cb-2",
        "text": STALE_BUTTON_REPLY,
    }
    assert len(telegram.sent_texts) == 2
    assert all(status is EventStatus.DONE for _, status in await _statuses(session))


async def test_redelivered_press_is_handled_once(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    worker = _worker(database, telegram, Clock())
    await _ingest(database, message_update(1, sender_id=OWNER))
    await worker.run_until_idle()
    data = _button_data(telegram.calls_to("send_message")[0])

    press = callback_update(2, sender_id=OWNER, callback_id="cb-1", data=data)
    assert await _ingest(database, press) is not None
    assert await _ingest(database, press) is None
    await worker.run_until_idle()

    assert len(telegram.calls_to("answer_callback_query")) == 1
    assert len(telegram.sent_texts) == 2


async def test_expired_button_is_rejected(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    clock = Clock()
    worker = _worker(database, telegram, clock)
    await _ingest(database, message_update(1, sender_id=OWNER))
    await worker.run_until_idle()
    data = _button_data(telegram.calls_to("send_message")[0])

    clock.advance(timedelta(hours=25))
    await _ingest(database, callback_update(2, sender_id=OWNER, callback_id="cb", data=data))
    await worker.run_until_idle()

    assert telegram.calls_to("answer_callback_query")[-1]["text"] == STALE_BUTTON_REPLY
    assert len(telegram.sent_texts) == 1


async def test_unrecognized_button_data_is_rejected(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    await _ingest(database, callback_update(1, sender_id=OWNER, callback_id="cb", data="junk"))
    await _worker(database, telegram, Clock()).run_until_idle()

    assert telegram.calls_to("answer_callback_query") == [
        {"callback_query_id": "cb", "text": STALE_BUTTON_REPLY}
    ]
    assert telegram.sent_texts == []


async def test_non_text_message_gets_a_text_only_reply(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    await _ingest(database, message_update(1, sender_id=OWNER, text=None))
    await _worker(database, telegram, Clock()).run_until_idle()

    [send] = telegram.calls_to("send_message")
    assert send["text"] == NOT_TEXT_REPLY
    assert send["reply_markup"] is None


async def test_long_reply_is_split_with_the_button_on_the_last_part(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    await _ingest(database, message_update(1, sender_id=OWNER, text="x" * MAX_MESSAGE_LENGTH))
    await _worker(database, telegram, Clock()).run_until_idle()

    first, second = telegram.calls_to("send_message")
    assert first["reply_markup"] is None
    assert second["reply_markup"] is not None
    assert (first["text"] + second["text"]) == "You said: " + "x" * MAX_MESSAGE_LENGTH


async def test_temporary_failure_retries_only_the_delivery(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    telegram.fail("send_message", TelegramTemporaryError("down"))
    clock = Clock()
    worker = _worker(database, telegram, clock)

    await _ingest(database, message_update(1, sender_id=OWNER))
    await worker.run_until_idle()
    assert await _statuses(session) == [
        (EventType.USER_MESSAGE, EventStatus.DONE),
        (EventType.TELEGRAM_OUTBOUND, EventStatus.PENDING),
    ]

    clock.advance(timedelta(minutes=1))
    await worker.run_until_idle()
    assert telegram.sent_texts == ["You said: hello"]
    # The handler ran once: still exactly one button.
    assert len((await session.scalars(select(PendingAction))).all()) == 1


async def test_permanent_delivery_failure_notifies_the_user(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    telegram.fail("send_message", TelegramRequestError(400, "Bad Request: something"))
    worker = _worker(database, telegram, Clock())

    await _ingest(database, message_update(1, sender_id=OWNER))
    await worker.run_until_idle()
    # The dead-event notice was queued after the worker went idle.
    await worker.run_until_idle()

    [notice] = telegram.sent_texts
    assert "went wrong" in notice
    assert [s for _, s in await _statuses(session)] == [
        EventStatus.DONE,
        EventStatus.DEAD,
        EventStatus.DONE,
    ]


async def test_a_failed_notice_is_not_reported_again(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    telegram.fail(
        "send_message",
        TelegramRequestError(403, "Forbidden: bot was blocked by the user"),
        TelegramRequestError(403, "Forbidden: bot was blocked by the user"),
    )
    worker = _worker(database, telegram, Clock())

    await _ingest(database, message_update(1, sender_id=OWNER))
    for _ in range(3):
        await worker.run_until_idle()

    assert telegram.sent_texts == []
    assert [s for _, s in await _statuses(session)] == [
        EventStatus.DONE,
        EventStatus.DEAD,
        EventStatus.DEAD,
    ]


async def test_rejected_tidy_up_calls_are_not_failures(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    telegram.fail("answer_callback_query", TelegramRequestError(400, "query is too old"))
    telegram.fail("edit_message_reply_markup", TelegramRequestError(400, "message is not modified"))

    await _ingest(database, callback_update(1, sender_id=OWNER, callback_id="cb", data="junk"))
    await _worker(database, telegram, Clock()).run_until_idle()

    assert all(status is EventStatus.DONE for _, status in await _statuses(session))
    assert telegram.sent_texts == []

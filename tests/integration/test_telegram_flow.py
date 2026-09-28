"""Telegram end to end with a fake Bot API.

A raw update becomes an event, its handler queues replies, and the worker delivers them.
Buttons are exercised through the /cancel confirmation; the intake conversation
has its own tests (test_intake_flow.py).

Runs against a real database (SQLite, and Postgres in CI) and the real worker.
"""

import uuid
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions import service as action_service
from app.actions.models import ActionKind, ActionStatus, PendingAction
from app.actions.service import ButtonSpec
from app.cases import service as case_service
from app.cases.models import CaseStatus, SupportCase, TransitionActor
from app.cases.state_machine import transition
from app.chat.handlers import (
    CANCELLED_REPLY,
    HELP_TEXT,
    KEPT_REPLY,
    NO_CASE_TO_CANCEL_REPLY,
    NOT_TEXT_REPLY,
    STALE_BUTTON_REPLY,
    UNKNOWN_COMMAND_REPLY,
)
from app.db.session import Database
from app.events.models import Event, EventStatus, EventType
from app.events.routing import build_registry
from app.events.service import EventPolicy
from app.events.worker import EventWorker
from app.telegram.delivery import MAX_MESSAGE_LENGTH, TelegramOutbox
from app.telegram.errors import TelegramRequestError, TelegramTemporaryError
from app.telegram.ingest import ingest_update
from app.telegram.keyboards import encode_callback_data
from app.telegram.notifier import TelegramDeadEventNotifier
from app.users.models import User
from tests.fakes import (
    Clock,
    FakeGmailClient,
    FakeLLMClient,
    FakeTelegramClient,
    callback_update,
    message_update,
)

OWNER = 1_000_000_001  # the `user` fixture's Telegram id


def _worker(
    database: Database,
    telegram: FakeTelegramClient,
    clock: Clock,
    llm: FakeLLMClient | None = None,
) -> EventWorker:
    return EventWorker(
        database,
        build_registry(
            telegram,
            llm or FakeLLMClient(),
            gmail=FakeGmailClient(),
            database=database,
            user_timezone=ZoneInfo("UTC"),
        ),
        EventPolicy(),
        notifier=TelegramDeadEventNotifier(database),
        poll_interval=0.01,
        clock=clock,
    )


async def _ingest(database: Database, raw: dict[str, Any]) -> int | None:
    async with database.transaction() as session:
        return await ingest_update(session, raw, allowed_user_id=OWNER)


async def _focused_case(database: Database, user: User) -> uuid.UUID:
    async with database.transaction() as session:
        case = await case_service.create_case(session, user_id=user.id, actor=TransitionActor.USER)
        await case_service.focus_case(session, case)
        return case.id


def _buttons(send_call: dict[str, Any]) -> dict[str, str]:
    """Label -> callback data for the buttons on a sent message."""
    [row] = send_call["reply_markup"]["inline_keyboard"]
    return {button["text"]: button["callback_data"] for button in row}


async def _statuses(session: AsyncSession) -> list[tuple[EventType, EventStatus]]:
    session.expire_all()
    rows = await session.scalars(select(Event).order_by(Event.id))
    return [(e.type, e.status) for e in rows.all()]


async def _case_status(session: AsyncSession, case_id: uuid.UUID) -> CaseStatus:
    session.expire_all()
    case = await session.get(SupportCase, case_id)
    assert case is not None
    return case.status


async def test_cancel_confirmation_buttons_work_once(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    worker = _worker(database, telegram, Clock())
    case_id = await _focused_case(database, user)

    await _ingest(database, message_update(1, sender_id=OWNER, text="/cancel"))
    await worker.run_until_idle()

    [send] = telegram.calls_to("send_message")
    assert send["chat_id"] == OWNER
    buttons = _buttons(send)
    assert list(buttons) == ["Yes, cancel", "Keep it"]
    actions = (await session.scalars(select(PendingAction).order_by(PendingAction.position))).all()
    assert [encode_callback_data(a.id) for a in actions] == list(buttons.values())
    assert all(a.case_id == case_id for a in actions)
    message_id = actions[0].telegram_message_id
    assert message_id is not None

    # Press "Yes, cancel".
    await _ingest(
        database,
        callback_update(
            2,
            sender_id=OWNER,
            callback_id="cb-1",
            data=buttons["Yes, cancel"],
            message_id=message_id,
        ),
    )
    await worker.run_until_idle()

    assert telegram.calls_to("edit_message_reply_markup") == [
        {"chat_id": OWNER, "message_id": message_id, "reply_markup": {"inline_keyboard": []}}
    ]
    assert telegram.calls_to("answer_callback_query") == [
        {"callback_query_id": "cb-1", "text": None}
    ]
    assert telegram.sent_texts[-1] == CANCELLED_REPLY.format(merchant="support")
    assert await _case_status(session, case_id) is CaseStatus.CANCELLED
    for action in actions:
        await session.refresh(action)
    assert [a.status for a in actions] == [ActionStatus.CONSUMED, ActionStatus.SUPERSEDED]

    # Press "Keep it" from the same message (a new callback query): rejected.
    await _ingest(
        database,
        callback_update(3, sender_id=OWNER, callback_id="cb-2", data=buttons["Keep it"]),
    )
    await worker.run_until_idle()

    assert telegram.calls_to("answer_callback_query")[-1] == {
        "callback_query_id": "cb-2",
        "text": STALE_BUTTON_REPLY,
    }
    assert len(telegram.sent_texts) == 2
    assert all(status is EventStatus.DONE for _, status in await _statuses(session))


async def test_keep_it_leaves_the_case_open(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    worker = _worker(database, telegram, Clock())
    case_id = await _focused_case(database, user)
    await _ingest(database, message_update(1, sender_id=OWNER, text="/cancel"))
    await worker.run_until_idle()
    keep = _buttons(telegram.calls_to("send_message")[0])["Keep it"]

    await _ingest(database, callback_update(2, sender_id=OWNER, callback_id="cb", data=keep))
    await worker.run_until_idle()

    assert telegram.sent_texts[-1] == KEPT_REPLY
    assert await _case_status(session, case_id) is CaseStatus.GATHERING_CONTEXT


async def test_cancel_confirmation_is_void_once_the_case_moves_on(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    worker = _worker(database, telegram, Clock())
    case_id = await _focused_case(database, user)
    await _ingest(database, message_update(1, sender_id=OWNER, text="/cancel"))
    await worker.run_until_idle()
    yes = _buttons(telegram.calls_to("send_message")[0])["Yes, cancel"]

    async with database.transaction() as s:
        case = await case_service.get_case(s, case_id, user_id=user.id)

        await transition(
            s, case, CaseStatus.READY_TO_DRAFT, reason="test", actor=TransitionActor.SYSTEM
        )

    await _ingest(database, callback_update(2, sender_id=OWNER, callback_id="cb", data=yes))
    await worker.run_until_idle()

    assert telegram.calls_to("answer_callback_query")[-1]["text"] == STALE_BUTTON_REPLY
    assert await _case_status(session, case_id) is CaseStatus.READY_TO_DRAFT


async def test_cancel_without_a_case(database: Database, session: AsyncSession, user: User) -> None:
    telegram = FakeTelegramClient()
    await _ingest(database, message_update(1, sender_id=OWNER, text="/cancel"))
    await _worker(database, telegram, Clock()).run_until_idle()

    [send] = telegram.calls_to("send_message")
    assert send["text"] == NO_CASE_TO_CANCEL_REPLY
    assert send["reply_markup"] is None


async def test_help_and_unknown_commands_need_no_llm(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    llm = FakeLLMClient()  # unscripted: any call would fail the event
    await _ingest(database, message_update(1, sender_id=OWNER, message_id=1, text="/start"))
    await _ingest(database, message_update(2, sender_id=OWNER, message_id=2, text="/help@my_bot"))
    await _ingest(database, message_update(3, sender_id=OWNER, message_id=3, text="/frobnicate"))
    await _worker(database, telegram, Clock(), llm).run_until_idle()

    assert telegram.sent_texts == [HELP_TEXT, HELP_TEXT, UNKNOWN_COMMAND_REPLY]
    assert llm.calls == []
    assert (await session.scalars(select(SupportCase))).all() == []


async def test_redelivered_press_is_handled_once(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    worker = _worker(database, telegram, Clock())
    await _focused_case(database, user)
    await _ingest(database, message_update(1, sender_id=OWNER, text="/cancel"))
    await worker.run_until_idle()
    keep = _buttons(telegram.calls_to("send_message")[0])["Keep it"]

    press = callback_update(2, sender_id=OWNER, callback_id="cb-1", data=keep)
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
    case_id = await _focused_case(database, user)
    await _ingest(database, message_update(1, sender_id=OWNER, text="/cancel"))
    await worker.run_until_idle()
    yes = _buttons(telegram.calls_to("send_message")[0])["Yes, cancel"]

    clock.advance(timedelta(hours=25))
    await _ingest(database, callback_update(2, sender_id=OWNER, callback_id="cb", data=yes))
    await worker.run_until_idle()

    assert telegram.calls_to("answer_callback_query")[-1]["text"] == STALE_BUTTON_REPLY
    assert len(telegram.sent_texts) == 1
    assert await _case_status(session, case_id) is CaseStatus.GATHERING_CONTEXT


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


async def test_long_message_is_split_with_the_buttons_on_the_last_part(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    text = "x" * (MAX_MESSAGE_LENGTH + 10)
    async with database.transaction() as s:
        group_id = await action_service.create_group(
            s, user_id=user.id, buttons=[ButtonSpec(ActionKind.KEEP_CASE, "OK")]
        )
        outbox = TelegramOutbox(s, user_id=user.id, chat_id=OWNER, key_prefix="test")
        await outbox.send_message(text, action_group_id=group_id)
    await _worker(database, telegram, Clock()).run_until_idle()

    first, second = telegram.calls_to("send_message")
    assert first["reply_markup"] is None
    assert second["reply_markup"] is not None
    assert first["text"] + second["text"] == text


async def test_temporary_failure_retries_only_the_delivery(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    telegram.fail("send_message", TelegramTemporaryError("down"))
    clock = Clock()
    worker = _worker(database, telegram, clock)
    await _focused_case(database, user)

    await _ingest(database, message_update(1, sender_id=OWNER, text="/cancel"))
    await worker.run_until_idle()
    assert await _statuses(session) == [
        (EventType.USER_MESSAGE, EventStatus.DONE),
        (EventType.TELEGRAM_OUTBOUND, EventStatus.PENDING),
    ]

    clock.advance(timedelta(minutes=1))
    await worker.run_until_idle()
    assert len(telegram.sent_texts) == 1
    # The handler ran once: still exactly one pair of buttons.
    assert len((await session.scalars(select(PendingAction))).all()) == 2


async def test_permanent_delivery_failure_notifies_the_user(
    database: Database, session: AsyncSession, user: User
) -> None:
    telegram = FakeTelegramClient()
    telegram.fail("send_message", TelegramRequestError(400, "Bad Request: something"))
    worker = _worker(database, telegram, Clock())

    await _ingest(database, message_update(1, sender_id=OWNER, text="/help"))
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

    await _ingest(database, message_update(1, sender_id=OWNER, text="/help"))
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

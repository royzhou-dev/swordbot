"""Drives the chat end to end: Telegram update -> worker -> fake LLM -> database.

Shared by the intake and drafting flow tests. Runs the real worker against a
real database with fake Telegram and LLM clients. The decision builders below
produce the dicts the fake LLM returns; tests assert on actions, states and
recorded facts, never on the model's wording.
"""

from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.models import PendingAction
from app.cases import service as case_service
from app.cases.models import CaseFact, SupportCase
from app.db.session import Database
from app.email.models import OutboundEmail
from app.events.routing import build_registry
from app.events.service import EventPolicy
from app.events.worker import EventWorker
from app.telegram.ingest import ingest_update
from app.telegram.keyboards import decode_callback_data
from app.telegram.notifier import TelegramDeadEventNotifier
from tests.fakes import (
    Clock,
    FakeGmailClient,
    FakeLLMClient,
    FakeTelegramClient,
    callback_update,
    message_update,
)

OWNER = 1_000_000_001  # the `user` fixture's Telegram id

COMPLAINT = "My DoorDash order tonight was missing the fries"
QUESTION = "What would you like DoorDash to do?"
SUPPORT = "support@doordash.com"
ANSWER = f"A refund please. Their support email is {SUPPORT}"
SUBJECT = "Missing fries from my order"
BODY = "Hi,\n\nMy DoorDash order tonight was missing the fries.\n\nCould you refund them?"


# --- Decisions the fake LLM returns -----------------------------------------------


# A fact the model reports: its value, or `(value, quote)` when the value isn't
# the user's exact words. A bare value quotes itself.
type Fact = str | tuple[str, str]


def _fact(key: str, fact: Fact) -> dict[str, str]:
    value, quote = fact if isinstance(fact, tuple) else (fact, fact)
    return {"key": key, "quote": quote, "value": value}


def _decision(action: dict[str, Any], **facts: Fact) -> dict[str, Any]:
    return {
        "facts": [_fact(key, fact) for key, fact in facts.items()],
        "action": action,
        "reason": "test",
    }


def ask(question: str = QUESTION, **facts: Fact) -> dict[str, Any]:
    return _decision({"tool": "ask_user", "question": question}, **facts)


def reply(text: str, **facts: Fact) -> dict[str, Any]:
    return _decision({"tool": "reply_to_user", "text": text}, **facts)


def finish(**facts: Fact) -> dict[str, Any]:
    return _decision({"tool": "finish_intake"}, **facts)


def email(subject: str = SUBJECT, body: str = BODY) -> dict[str, Any]:
    """A `draft_support_email` action: the drafter's whole output."""
    return {"tool": "draft_support_email", "subject": subject, "body": body}


def revise(subject: str = SUBJECT, body: str = BODY, **facts: Fact) -> dict[str, Any]:
    """A draft-review decision that writes a new version."""
    return _decision(email(subject, body), **facts)


def complaint_facts(today: str) -> dict[str, Fact]:
    """What the model reads in COMPLAINT, each with the words it quotes."""
    return {
        "merchant_name": "DoorDash",
        "issue_type": ("missing_item", "missing the fries"),
        "issue_summary": (
            "The order was missing the fries.",
            "order tonight was missing the fries",
        ),
        "order_date": (today, "tonight"),
        "missing_items": "fries",
    }


SENDER = "me@example.com"


class Harness:
    def __init__(
        self, database: Database, session: AsyncSession, *, policy: EventPolicy | None = None
    ) -> None:
        self.database = database
        self.session = session
        self.telegram = FakeTelegramClient()
        self.llm = FakeLLMClient()
        self.gmail = FakeGmailClient()
        self.clock = Clock()
        self.worker = EventWorker(
            database,
            build_registry(
                self.telegram,
                self.llm,
                gmail=self.gmail,
                database=database,
                user_timezone=ZoneInfo("UTC"),
                sender_address=SENDER,
            ),
            policy or EventPolicy(),
            notifier=TelegramDeadEventNotifier(database),
            poll_interval=0.01,
            clock=self.clock,
        )
        self._next_update = 0

    @property
    def today(self) -> str:
        return self.clock().date().isoformat()

    def _update_id(self) -> int:
        self._next_update += 1
        return self._next_update

    async def say(self, text: str) -> int:
        """Send a user message and run the worker until idle. Returns the message id."""
        message_id = self._update_id()
        async with self.database.transaction() as s:
            await ingest_update(
                s,
                message_update(message_id, sender_id=OWNER, message_id=message_id, text=text),
                allowed_user_id=OWNER,
            )
        await self.worker.run_until_idle()
        return message_id

    async def press(self, data: str, *, message_id: int | None = None) -> str:
        """Press a button (by its callback data) and run the worker. Returns the callback id."""
        update_id = self._update_id()
        callback_id = f"cb-{update_id}"
        if message_id is None:
            message_id = await self._message_of(data)
        async with self.database.transaction() as s:
            await ingest_update(
                s,
                callback_update(
                    update_id,
                    sender_id=OWNER,
                    callback_id=callback_id,
                    data=data,
                    message_id=message_id,
                ),
                allowed_user_id=OWNER,
            )
        await self.worker.run_until_idle()
        return callback_id

    async def _message_of(self, data: str) -> int | None:
        """The Telegram message the button was delivered on."""
        action_id = decode_callback_data(data)
        self.session.expire_all()
        action = await self.session.get(PendingAction, action_id) if action_id else None
        return action.telegram_message_id if action else None

    def buttons(self, index: int = -1) -> dict[str, str]:
        """Label -> callback data, for the `index`th sent message that had buttons."""
        with_buttons = [
            c for c in self.telegram.calls_to("send_message") if c["reply_markup"] is not None
        ]
        [row] = with_buttons[index]["reply_markup"]["inline_keyboard"]
        return {button["text"]: button["callback_data"] for button in row}

    def callback_answer(self, callback_id: str) -> str | None:
        [answer] = [
            c
            for c in self.telegram.calls_to("answer_callback_query")
            if c["callback_query_id"] == callback_id
        ]
        text: str | None = answer["text"]
        return text

    async def cases(self) -> list[SupportCase]:
        self.session.expire_all()
        return list((await self.session.scalars(select(SupportCase))).all())

    async def only_case(self) -> SupportCase:
        [case] = await self.cases()
        return case

    async def emails(self) -> list[OutboundEmail]:
        self.session.expire_all()
        rows = await self.session.scalars(select(OutboundEmail).order_by(OutboundEmail.version))
        return list(rows.all())

    async def facts(self, case: SupportCase) -> list[CaseFact]:
        rows = await self.session.scalars(
            select(CaseFact).where(CaseFact.case_id == case.id).order_by(CaseFact.id)
        )
        return list(rows.all())

    async def current(self, case: SupportCase) -> dict[str, Any]:
        facts = await case_service.get_current_facts(self.session, case)
        return {key: fact.value for key, fact in facts.items()}

    async def reach_draft(self, **draft: str) -> SupportCase:
        """Complaint -> question -> answer -> first draft shown."""
        self.llm.script(
            ask(**complaint_facts(self.today)),
            finish(desired_resolution="refund", support_email=SUPPORT),
            email(**draft),
        )
        await self.say(COMPLAINT)
        await self.say(ANSWER)
        return await self.only_case()

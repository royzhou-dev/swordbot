"""Test doubles for external services."""

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.email.gmail_client import GmailSent
from app.events.models import EventSource, EventType
from app.events.schemas import ClaimedEvent
from app.llm.client import Message
from app.llm.errors import InvalidAgentDecisionError
from app.logging import get_logger
from app.telegram.client import ReplyMarkup
from app.telegram.delivery import TelegramOutbox
from app.telegram.schemas import SentMessage, TelegramChat, TelegramUser
from app.tools.registry import ToolContext


@dataclass(frozen=True)
class TelegramCall:
    method: str
    args: dict[str, Any]


class FakeTelegramClient:
    """Records calls. `fail(method, *errors)` makes the next calls to `method` raise."""

    def __init__(self) -> None:
        self.calls: list[TelegramCall] = []
        # Batches returned by successive get_updates calls.
        self.update_batches: list[list[dict[str, Any]]] = []
        self._failures: defaultdict[str, list[Exception]] = defaultdict(list)
        self._next_message_id = 500

    def fail(self, method: str, *errors: Exception) -> None:
        self._failures[method].extend(errors)

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        return [c.args for c in self.calls if c.method == method]

    @property
    def sent_texts(self) -> list[str]:
        return [args["text"] for args in self.calls_to("send_message")]

    def _record(self, method: str, **args: Any) -> None:
        if self._failures[method]:
            raise self._failures[method].pop(0)
        self.calls.append(TelegramCall(method, args))

    async def get_me(self) -> TelegramUser:
        self._record("get_me")
        return TelegramUser(id=42, is_bot=True, username="test_bot")

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: ReplyMarkup | None = None
    ) -> SentMessage:
        self._record("send_message", chat_id=chat_id, text=text, reply_markup=reply_markup)
        self._next_message_id += 1
        return SentMessage(
            message_id=self._next_message_id, chat=TelegramChat(id=chat_id, type="private")
        )

    async def answer_callback_query(
        self, callback_query_id: str, *, text: str | None = None
    ) -> None:
        self._record("answer_callback_query", callback_query_id=callback_query_id, text=text)

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, *, reply_markup: ReplyMarkup | None = None
    ) -> None:
        self._record(
            "edit_message_reply_markup",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )

    async def get_updates(
        self, *, offset: int | None, poll_seconds: int, allowed_updates: list[str]
    ) -> list[dict[str, Any]]:
        self._record(
            "get_updates",
            offset=offset,
            poll_seconds=poll_seconds,
            allowed_updates=allowed_updates,
        )
        return self.update_batches.pop(0) if self.update_batches else []

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> None:
        self._record("delete_webhook", drop_pending_updates=drop_pending_updates)


class FakeGmailClient:
    """Records sent messages. Failures are scripted per call:

    - `fail_authorize(*errors)`: the next `authorize` calls raise (nothing sent);
    - `fail_send(*errors)`: the next sends raise *without* Gmail getting the message;
    - `accept_then(*outcomes)`: the next sends reach Gmail (recorded in `sent`),
      then raise the given error, or hang for the given number of seconds.

    `on_send`, if set, runs at the start of every send, e.g. to check what the
    database looks like at the moment Gmail is called.
    """

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.authorize_calls = 0
        self.on_send: Callable[[], Awaitable[None]] | None = None
        self._authorize_errors: list[Exception] = []
        self._send_errors: list[Exception] = []
        self._after_accept: list[Exception | float] = []

    def fail_authorize(self, *errors: Exception) -> None:
        self._authorize_errors.extend(errors)

    def fail_send(self, *errors: Exception) -> None:
        self._send_errors.extend(errors)

    def accept_then(self, *outcomes: Exception | float) -> None:
        self._after_accept.extend(outcomes)

    async def authorize(self) -> None:
        self.authorize_calls += 1
        if self._authorize_errors:
            raise self._authorize_errors.pop(0)

    async def send(self, raw: bytes) -> GmailSent:
        if self.on_send is not None:
            await self.on_send()
        if self._send_errors:
            raise self._send_errors.pop(0)
        self.sent.append(raw)
        number = len(self.sent)
        if self._after_accept:
            outcome = self._after_accept.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            await asyncio.sleep(outcome)
        return GmailSent(message_id=f"gmail-msg-{number}", thread_id=f"gmail-thread-{number}")

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True)
class LLMCall:
    method: str
    messages: list[Message]
    schema: type[BaseModel] | None
    purpose: str


# A scripted reply: a model instance or a dict (validated against the requested
# schema) for `extract_structured`, a str for `complete`, or an exception to raise.
type LLMReply = BaseModel | dict[str, Any] | str | Exception


class FakeLLMClient:
    """Answers from a script, in call order. An unscripted call fails the test.

    A dict reply that doesn't validate raises `InvalidAgentDecisionError`, as the
    real client does once its repair retry has failed.
    """

    def __init__(self) -> None:
        self.calls: list[LLMCall] = []
        self.closed = False
        self._replies: list[LLMReply] = []

    def script(self, *replies: LLMReply) -> None:
        self._replies.extend(replies)

    @property
    def unused_replies(self) -> int:
        return len(self._replies)

    async def complete(self, messages: list[Message], *, purpose: str) -> str:
        reply = self._next(LLMCall("complete", list(messages), None, purpose))
        if not isinstance(reply, str):
            raise AssertionError(f"complete({purpose}) was scripted a {type(reply).__name__}")
        return reply

    async def extract_structured[T: BaseModel](
        self, messages: list[Message], schema: type[T], *, purpose: str
    ) -> T:
        reply = self._next(LLMCall("extract_structured", list(messages), schema, purpose))
        if isinstance(reply, dict):
            try:
                return schema.model_validate(reply)
            except ValidationError as exc:
                locations = [".".join(str(p) for p in e["loc"]) for e in exc.errors()]
                raise InvalidAgentDecisionError(schema.__name__, locations) from None
        if not isinstance(reply, schema):
            raise AssertionError(
                f"extract_structured({purpose}) wants {schema.__name__}, "
                f"was scripted a {type(reply).__name__}"
            )
        return reply

    async def aclose(self) -> None:
        self.closed = True

    def _next(self, call: LLMCall) -> BaseModel | dict[str, Any] | str:
        self.calls.append(call)
        if not self._replies:
            raise AssertionError(f"unscripted LLM call: {call.method}({call.purpose})")
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def tool_context() -> ToolContext:
    """A context for tools that touch neither the database nor Telegram."""
    event = ClaimedEvent(
        id=1,
        user_id=uuid.uuid4(),
        case_id=None,
        type=EventType.USER_MESSAGE,
        source=EventSource.DEV,
        external_id="x",
        payload={},
        attempts=1,
        claim_token=uuid.uuid4(),
        created_at=datetime(2026, 9, 26, tzinfo=UTC),
    )
    return ToolContext(
        session=cast(AsyncSession, None),
        event=event,
        now=event.created_at,
        log=get_logger("test"),
        outbox=cast(TelegramOutbox, None),
    )


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


# --- Raw Telegram updates ---------------------------------------------------------


def message_update(
    update_id: int,
    *,
    sender_id: int,
    message_id: int = 1,
    text: str | None = "hello",
    chat_type: str = "private",
    is_bot: bool = False,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": message_id,
        "date": 1_790_000_000,
        "chat": {"id": sender_id, "type": chat_type},
        "from": {"id": sender_id, "is_bot": is_bot, "first_name": "Test", "last_name": "User"},
    }
    if text is not None:
        message["text"] = text
    else:
        message["sticker"] = {"file_id": "x"}
    return {"update_id": update_id, "message": message}


def callback_update(
    update_id: int,
    *,
    sender_id: int,
    callback_id: str,
    data: str | None,
    message_id: int | None = 501,
) -> dict[str, Any]:
    query: dict[str, Any] = {
        "id": callback_id,
        "from": {"id": sender_id, "is_bot": False, "first_name": "Test", "last_name": "User"},
        "chat_instance": "ci",
    }
    if data is not None:
        query["data"] = data
    if message_id is not None:
        query["message"] = {
            "message_id": message_id,
            "date": 1_790_000_000,
            "chat": {"id": sender_id, "type": "private"},
        }
    return {"update_id": update_id, "callback_query": query}

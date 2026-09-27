"""Outbound Telegram calls go through the event queue (PLAN D10).

Handlers never call Telegram directly. They queue each call as a
`TELEGRAM_OUTBOUND` event through a `TelegramOutbox`, in the same transaction
as the rest of their work, and `TelegramDelivery` makes the call when that
event runs. So:

- a Telegram outage retries only the delivery, never the handler (or, from M5,
  its LLM calls);
- a message with buttons is sent only after its `pending_actions` rows are
  committed;
- replies keep their order, because a user's events run one at a time.

Delivery is at-least-once: a crash between Telegram accepting a message and
the commit sends it again. That is acceptable for chat messages. Email sends
have their own guard (PLAN D2).
"""

import uuid
from collections.abc import Awaitable
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions import service as action_service
from app.events import service as event_service
from app.events.errors import PermanentEventError
from app.events.handlers import HandlerContext
from app.events.models import EventSource, EventType
from app.events.schemas import NewEvent
from app.telegram.client import ReplyMarkup, TelegramClient
from app.telegram.errors import TelegramRequestError
from app.telegram.keyboards import EMPTY_KEYBOARD, inline_keyboard
from app.users.models import User

# Telegram's limit, counted in UTF-16 code units.
MAX_MESSAGE_LENGTH = 4096


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SendMessageOp(_Op):
    op: Literal["send_message"] = "send_message"
    chat_id: int
    text: str = Field(min_length=1)
    # Buttons to attach: the group's actions that are still open at send time.
    action_group_id: uuid.UUID | None = None
    # A failure notice is never itself reported if it fails (no notice loops).
    failure_notice: bool = False


class AnswerCallbackOp(_Op):
    op: Literal["answer_callback"] = "answer_callback"
    callback_query_id: str
    # Shown briefly at the top of the chat.
    text: str | None = None


class ClearButtonsOp(_Op):
    op: Literal["clear_buttons"] = "clear_buttons"
    chat_id: int
    message_id: int


type OutboundOp = SendMessageOp | AnswerCallbackOp | ClearButtonsOp


class TelegramOutboundPayload(RootModel[Annotated[OutboundOp, Field(discriminator="op")]]):
    """The payload of a `TELEGRAM_OUTBOUND` event."""


def split_text(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into messages within Telegram's length limit, preferring line breaks."""
    chunks: list[str] = []
    rest = text
    while _utf16_length(rest) > limit:
        cut = _prefix_fitting(rest, limit)
        newline = rest.rfind("\n", 0, cut)
        if newline > 0:
            cut = newline + 1
        chunks.append(rest[:cut])
        rest = rest[cut:]
    chunks.append(rest)
    # Telegram rejects empty messages.
    return [c.rstrip("\n") for c in chunks if c.strip()]


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _prefix_fitting(text: str, limit: int) -> int:
    """The number of leading characters that fit in `limit` UTF-16 code units."""
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return index
    return len(text)


class TelegramOutbox:
    """Queues Telegram calls to one user, for one unit of work.

    `key_prefix` must be unique to that unit of work (for example the handled
    event's id). Each queued call's dedup key is the prefix plus a counter.
    `now` stamps the queued events; None means the current time.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        chat_id: int,
        key_prefix: str,
        now: datetime | None = None,
    ) -> None:
        self._session = session
        self._user_id = user_id
        self._chat_id = chat_id
        self._key_prefix = key_prefix
        self._now = now
        self._count = 0

    @classmethod
    async def for_event(cls, ctx: HandlerContext) -> "TelegramOutbox":
        """An outbox that replies to the user whose event is being handled."""
        user = await ctx.session.get(User, ctx.event.user_id)
        if user is None:
            raise PermanentEventError(f"user {ctx.event.user_id} not found")
        # The bot only talks in private chats, where the chat id is the user id.
        return cls(
            ctx.session,
            user_id=user.id,
            chat_id=user.telegram_user_id,
            key_prefix=f"event:{ctx.event.id}",
            now=ctx.now,
        )

    async def send_message(
        self,
        text: str,
        *,
        action_group_id: uuid.UUID | None = None,
        failure_notice: bool = False,
    ) -> None:
        """Queue a message, split if long. Buttons go on the last part."""
        chunks = split_text(text)
        if not chunks:
            raise ValueError("cannot send an empty message")
        for index, chunk in enumerate(chunks):
            last = index == len(chunks) - 1
            await self._enqueue(
                SendMessageOp(
                    chat_id=self._chat_id,
                    text=chunk,
                    action_group_id=action_group_id if last else None,
                    failure_notice=failure_notice,
                )
            )

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        await self._enqueue(AnswerCallbackOp(callback_query_id=callback_query_id, text=text))

    async def clear_buttons(self, message_id: int) -> None:
        await self._enqueue(ClearButtonsOp(chat_id=self._chat_id, message_id=message_id))

    async def _enqueue(self, op: OutboundOp) -> None:
        self._count += 1
        await event_service.enqueue(
            self._session,
            NewEvent(
                user_id=self._user_id,
                type=EventType.TELEGRAM_OUTBOUND,
                source=EventSource.SYSTEM,
                external_id=f"telegram:{self._key_prefix}:{self._count}",
                payload=op.model_dump(mode="json"),
            ),
            now=self._now,
        )


class TelegramDelivery:
    """The `TELEGRAM_OUTBOUND` handler: makes one queued Telegram call."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def __call__(self, ctx: HandlerContext, payload: TelegramOutboundPayload) -> None:
        op = payload.root
        match op:
            case SendMessageOp():
                await self._send_message(ctx, op)
            case AnswerCallbackOp():
                await self._cosmetic(
                    ctx,
                    "answerCallbackQuery",
                    self._client.answer_callback_query(op.callback_query_id, text=op.text),
                )
            case ClearButtonsOp():
                await self._cosmetic(
                    ctx,
                    "editMessageReplyMarkup",
                    self._client.edit_message_reply_markup(
                        op.chat_id, op.message_id, reply_markup=EMPTY_KEYBOARD
                    ),
                )

    async def _send_message(self, ctx: HandlerContext, op: SendMessageOp) -> None:
        markup: ReplyMarkup | None = None
        if op.action_group_id is not None:
            actions = await action_service.open_actions_in_group(
                ctx.session, op.action_group_id, user_id=ctx.event.user_id, now=ctx.now
            )
            # If every button was closed before delivery, send the text alone.
            if actions:
                markup = inline_keyboard(actions)
        sent = await self._client.send_message(op.chat_id, op.text, reply_markup=markup)
        if markup is not None and op.action_group_id is not None:
            await action_service.record_delivery(
                ctx.session, op.action_group_id, chat_id=op.chat_id, message_id=sent.message_id
            )
        ctx.log.info("telegram_message_sent", telegram_message_id=sent.message_id)

    async def _cosmetic(self, ctx: HandlerContext, method: str, call: Awaitable[None]) -> None:
        """Run a call that only tidies up the chat (acknowledging a press, removing buttons).

        Telegram rejecting it ("query is too old", "message is not modified")
        is not worth retrying or reporting. Network errors still retry.
        """
        try:
            await call
        except TelegramRequestError as exc:
            ctx.log.info("telegram_call_rejected", method=method, error_code=exc.error_code)

"""Test doubles for external services."""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from app.telegram.client import ReplyMarkup
from app.telegram.schemas import SentMessage, TelegramChat, TelegramUser


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
        "from": {"id": sender_id, "is_bot": is_bot, "first_name": "Test"},
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
        "from": {"id": sender_id, "is_bot": False, "first_name": "Test"},
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

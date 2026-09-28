"""A thin async client for the Telegram Bot API (PLAN D4).

Only the methods the app needs. Every call is a JSON POST to
`{base_url}/bot{token}/{method}`. Because the token is in the URL, errors never
include the URL or chain the underlying httpx exception, and the `httpx` logger
is kept at WARNING (see `app.logging`).
"""

from typing import Any, NoReturn, Protocol

import httpx
from pydantic import SecretStr

from app.telegram.errors import (
    TelegramAuthenticationError,
    TelegramRateLimitError,
    TelegramRequestError,
    TelegramTemporaryError,
)
from app.telegram.schemas import SentMessage, TelegramUser, WebhookInfo

type ReplyMarkup = dict[str, Any]

_DEFAULT_TIMEOUT = 15.0
_MAX_DESCRIPTION_LENGTH = 200


class TelegramClient(Protocol):
    async def get_me(self) -> TelegramUser: ...

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: ReplyMarkup | None = None
    ) -> SentMessage: ...

    async def answer_callback_query(
        self, callback_query_id: str, *, text: str | None = None
    ) -> None: ...

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, *, reply_markup: ReplyMarkup | None = None
    ) -> None: ...

    async def get_updates(
        self, *, offset: int | None, poll_seconds: int, allowed_updates: list[str]
    ) -> list[dict[str, Any]]: ...

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> None: ...

    async def get_webhook_info(self) -> WebhookInfo: ...


class HttpTelegramClient:
    """The real client. The caller owns `http` and closes it."""

    def __init__(
        self,
        token: SecretStr,
        http: httpx.AsyncClient,
        *,
        base_url: str = "https://api.telegram.org",
    ) -> None:
        self._token = token
        self._http = http
        self._base_url = base_url.rstrip("/")

    async def get_me(self) -> TelegramUser:
        return TelegramUser.model_validate(await self._call("getMe", {}))

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: ReplyMarkup | None = None
    ) -> SentMessage:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return SentMessage.model_validate(await self._call("sendMessage", params))

    async def answer_callback_query(
        self, callback_query_id: str, *, text: str | None = None
    ) -> None:
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            params["text"] = text
        await self._call("answerCallbackQuery", params)

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, *, reply_markup: ReplyMarkup | None = None
    ) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        await self._call("editMessageReplyMarkup", params)

    async def get_updates(
        self, *, offset: int | None, poll_seconds: int, allowed_updates: list[str]
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": poll_seconds, "allowed_updates": allowed_updates}
        if offset is not None:
            params["offset"] = offset
        # The HTTP timeout must outlast Telegram's long-poll timeout.
        result = await self._call("getUpdates", params, http_timeout=poll_seconds + 10)
        if not isinstance(result, list):
            raise TelegramTemporaryError("getUpdates returned an unexpected result")
        return result

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> None:
        await self._call("deleteWebhook", {"drop_pending_updates": drop_pending_updates})

    async def get_webhook_info(self) -> WebhookInfo:
        return WebhookInfo.model_validate(await self._call("getWebhookInfo", {}))

    async def set_webhook(
        self,
        url: str,
        *,
        secret_token: SecretStr,
        allowed_updates: list[str],
        drop_pending_updates: bool = False,
    ) -> None:
        """Register the production webhook. Only `scripts/telegram_webhook.py` calls it."""
        await self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token.get_secret_value(),
                "allowed_updates": allowed_updates,
                "drop_pending_updates": drop_pending_updates,
            },
        )

    async def _call(
        self, method: str, params: dict[str, Any], *, http_timeout: float = _DEFAULT_TIMEOUT
    ) -> Any:
        url = f"{self._base_url}/bot{self._token.get_secret_value()}/{method}"
        try:
            response = await self._http.post(url, json=params, timeout=http_timeout)
        except httpx.HTTPError as exc:
            # `from None`: the httpx exception's message contains the URL, and so the token.
            raise TelegramTemporaryError(f"{method}: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            if response.status_code >= 500 or response.status_code == 429:
                raise TelegramTemporaryError(f"{method}: HTTP {response.status_code}")
            raise TelegramRequestError(response.status_code, "response is not JSON")
        if body.get("ok") is True:
            return body.get("result")
        _raise_for_error(method, response.status_code, body)


def _raise_for_error(method: str, status_code: int, body: dict[str, Any]) -> NoReturn:
    code = body.get("error_code")
    error_code = code if isinstance(code, int) else status_code
    description = str(body.get("description") or "no description")[:_MAX_DESCRIPTION_LENGTH]
    if error_code == 429:
        parameters = body.get("parameters")
        retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
        raise TelegramRateLimitError(retry_after if isinstance(retry_after, int) else None)
    if error_code >= 500:
        raise TelegramTemporaryError(f"{method}: {error_code} {description}")
    # A wrong token gets 401 Unauthorized, or 404 Not Found for a malformed one.
    if error_code in (401, 404):
        raise TelegramAuthenticationError(f"{method}: {error_code} {description}")
    raise TelegramRequestError(error_code, f"{method}: {description}")


class UnconfiguredTelegramClient:
    """Used when TELEGRAM_BOT_TOKEN is unset. Every call fails permanently."""

    def _fail(self) -> TelegramAuthenticationError:
        return TelegramAuthenticationError("TELEGRAM_BOT_TOKEN is not set")

    async def get_me(self) -> TelegramUser:
        raise self._fail()

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: ReplyMarkup | None = None
    ) -> SentMessage:
        raise self._fail()

    async def answer_callback_query(
        self, callback_query_id: str, *, text: str | None = None
    ) -> None:
        raise self._fail()

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, *, reply_markup: ReplyMarkup | None = None
    ) -> None:
        raise self._fail()

    async def get_updates(
        self, *, offset: int | None, poll_seconds: int, allowed_updates: list[str]
    ) -> list[dict[str, Any]]:
        raise self._fail()

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> None:
        raise self._fail()

    async def get_webhook_info(self) -> WebhookInfo:
        raise self._fail()

"""The HTTP Telegram client against mocked Bot API responses (respx)."""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.events.errors import PermanentEventError
from app.telegram.client import HttpTelegramClient, UnconfiguredTelegramClient
from app.telegram.errors import (
    TelegramAuthenticationError,
    TelegramError,
    TelegramRateLimitError,
    TelegramRequestError,
    TelegramTemporaryError,
)

BASE = "https://telegram.test"
TOKEN = "123456789:AAFakeTokenFakeTokenFakeTokenFake12"


def _url(method: str) -> str:
    return f"{BASE}/bot{TOKEN}/{method}"


def _ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def _error(code: int, description: str, **extra: object) -> httpx.Response:
    return httpx.Response(
        code, json={"ok": False, "error_code": code, "description": description, **extra}
    )


@pytest.fixture
async def client() -> AsyncIterator[HttpTelegramClient]:
    async with httpx.AsyncClient() as http:
        yield HttpTelegramClient(SecretStr(TOKEN), http, base_url=BASE)


async def test_send_message_posts_json_and_parses_the_result(
    client: HttpTelegramClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(_url("sendMessage")).mock(
        return_value=_ok({"message_id": 7, "chat": {"id": 1, "type": "private"}, "date": 0})
    )
    markup = {"inline_keyboard": [[{"text": "Go", "callback_data": "a:1"}]]}

    sent = await client.send_message(1, "hi", reply_markup=markup)

    assert sent.message_id == 7
    body = json.loads(route.calls.last.request.content)
    assert body == {"chat_id": 1, "text": "hi", "reply_markup": markup}


async def test_get_updates_sends_long_poll_parameters(
    client: HttpTelegramClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(_url("getUpdates")).mock(return_value=_ok([{"update_id": 3}]))

    updates = await client.get_updates(offset=3, poll_seconds=30, allowed_updates=["message"])

    assert updates == [{"update_id": 3}]
    body = json.loads(route.calls.last.request.content)
    assert body == {"offset": 3, "timeout": 30, "allowed_updates": ["message"]}


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (_error(429, "Too Many Requests", parameters={"retry_after": 7}), TelegramRateLimitError),
        (_error(500, "Internal Server Error"), TelegramTemporaryError),
        (httpx.Response(502, text="<html>bad gateway</html>"), TelegramTemporaryError),
        (_error(400, "Bad Request: chat not found"), TelegramRequestError),
        (_error(403, "Forbidden: bot was blocked by the user"), TelegramRequestError),
        (_error(401, "Unauthorized"), TelegramAuthenticationError),
        (_error(404, "Not Found"), TelegramAuthenticationError),
    ],
)
async def test_error_responses_map_to_typed_errors(
    client: HttpTelegramClient,
    respx_mock: respx.MockRouter,
    response: httpx.Response,
    error_type: type[TelegramError],
) -> None:
    respx_mock.post(_url("sendMessage")).mock(return_value=response)

    with pytest.raises(error_type) as info:
        await client.send_message(1, "hi")

    assert TOKEN not in str(info.value)
    permanent = isinstance(info.value, PermanentEventError)
    assert permanent == (not isinstance(info.value, TelegramTemporaryError))


async def test_rate_limit_keeps_retry_after(
    client: HttpTelegramClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(_url("sendMessage")).mock(
        return_value=_error(429, "Too Many Requests", parameters={"retry_after": 7})
    )
    with pytest.raises(TelegramRateLimitError) as info:
        await client.send_message(1, "hi")
    assert info.value.retry_after == 7


async def test_request_error_keeps_telegrams_description(
    client: HttpTelegramClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(_url("sendMessage")).mock(
        return_value=_error(403, "Forbidden: bot was blocked by the user")
    )
    with pytest.raises(TelegramRequestError) as info:
        await client.send_message(1, "hi")
    assert info.value.error_code == 403
    assert "blocked" in info.value.description


async def test_network_error_is_temporary_and_hides_the_url(
    client: HttpTelegramClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(_url("sendMessage")).mock(
        side_effect=httpx.ConnectError(f"cannot reach {_url('sendMessage')}")
    )

    with pytest.raises(TelegramTemporaryError) as info:
        await client.send_message(1, "hi")

    # The httpx exception (whose message has the URL, so the token) is not chained.
    assert TOKEN not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


async def test_unconfigured_client_fails_permanently() -> None:
    with pytest.raises(TelegramAuthenticationError):
        await UnconfiguredTelegramClient().send_message(1, "hi")

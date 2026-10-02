"""The HTTP Gmail client against mocked Google responses (respx)."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.config import Settings
from app.email.errors import (
    GmailAuthenticationError,
    GmailRejectedError,
    GmailTemporaryError,
    GmailUnreachableError,
    provably_not_sent,
)
from app.email.gmail_client import (
    GMAIL_READ_SCOPE,
    GMAIL_SEND_SCOPE,
    GmailRef,
    HttpGmailClient,
    UnconfiguredGmailClient,
    build_gmail_client,
)
from app.events.errors import PermanentEventError

TOKEN_URL = "https://oauth.test/token"
API = "https://gmail.test"
SEND_URL = f"{API}/gmail/v1/users/me/messages/send"
MESSAGES_URL = f"{API}/gmail/v1/users/me/messages"
BOTH_SCOPES = f"{GMAIL_SEND_SCOPE} {GMAIL_READ_SCOPE}"
REFRESH_TOKEN = "1//refresh-token-value"
CLIENT_SECRET = "client-secret-value"
ACCESS_TOKEN = "ya29.access-token-value"
RAW = b"To: support@example.com\r\nSubject: Order 123\r\n\r\nHi,\r\n"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _token_response(
    token: str = ACCESS_TOKEN, *, expires_in: int = 3599, scope: str = GMAIL_SEND_SCOPE
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": token,
            "expires_in": expires_in,
            "scope": scope,
            "token_type": "Bearer",
        },
    )


def _google_error(
    status: int, reason: str, api_status: str = "FAILED_PRECONDITION"
) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "error": {
                "code": status,
                "message": "Something about support@example.com",
                "errors": [{"message": "details", "domain": "global", "reason": reason}],
                "status": api_status,
            }
        },
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def client(clock: Clock) -> AsyncIterator[HttpGmailClient]:
    async with httpx.AsyncClient() as http:
        yield HttpGmailClient(
            client_id="client-id.apps.googleusercontent.com",
            client_secret=SecretStr(CLIENT_SECRET),
            refresh_token=SecretStr(REFRESH_TOKEN),
            http=http,
            token_url=TOKEN_URL,
            api_base_url=API,
            clock=clock,
        )


def _no_secrets(exc: BaseException) -> None:
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET, "support@example.com"):
        assert secret not in str(exc)
    assert exc.__cause__ is None


# --- token refresh --------------------------------------------------------------------


async def test_authorize_refreshes_with_the_refresh_token_grant(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(TOKEN_URL).mock(return_value=_token_response())

    await client.authorize()

    form = parse_qs(route.calls.last.request.content.decode())
    assert form == {
        "grant_type": ["refresh_token"],
        "refresh_token": [REFRESH_TOKEN],
        "client_id": ["client-id.apps.googleusercontent.com"],
        "client_secret": [CLIENT_SECRET],
    }


async def test_a_token_is_reused_until_shortly_before_it_expires(
    client: HttpGmailClient, clock: Clock, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(TOKEN_URL).mock(return_value=_token_response(expires_in=3600))

    await client.authorize()
    clock.now += 3000  # 10 minutes left: still fine
    await client.authorize()
    assert route.call_count == 1

    clock.now += 400  # within the 5-minute margin
    await client.authorize()
    assert route.call_count == 2


async def test_concurrent_authorize_calls_refresh_once(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(TOKEN_URL).mock(return_value=_token_response())

    await asyncio.gather(*(client.authorize() for _ in range(5)))

    assert route.call_count == 1


@pytest.mark.parametrize("error", ["invalid_grant", "invalid_client", "unauthorized_client"])
async def test_a_rejected_refresh_token_is_an_authentication_error(
    client: HttpGmailClient, respx_mock: respx.MockRouter, error: str
) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            400, json={"error": error, "error_description": "Token has been expired or revoked."}
        )
    )

    with pytest.raises(GmailAuthenticationError, match=error) as caught:
        await client.authorize()

    assert isinstance(caught.value, PermanentEventError)
    _no_secrets(caught.value)


async def test_a_token_without_the_send_scope_is_an_authentication_error(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=_token_response(scope="https://www.googleapis.com/auth/gmail.readonly")
    )

    with pytest.raises(GmailAuthenticationError, match="scope"):
        await client.authorize()


@pytest.mark.parametrize("status", [500, 503, 429])
async def test_a_token_endpoint_outage_is_temporary(
    client: HttpGmailClient, respx_mock: respx.MockRouter, status: int
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=httpx.Response(status, text="busy"))

    with pytest.raises(GmailTemporaryError) as caught:
        await client.authorize()

    assert not isinstance(caught.value, PermanentEventError)


async def test_an_unreachable_token_endpoint_is_unreachable(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(GmailUnreachableError) as caught:
        await client.authorize()

    _no_secrets(caught.value)


# --- send -----------------------------------------------------------------------------


async def test_send_posts_the_raw_message_and_returns_gmails_ids(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx_mock.post(SEND_URL).mock(
        return_value=httpx.Response(200, json={"id": "m1", "threadId": "t1", "labelIds": ["SENT"]})
    )

    await client.authorize()
    sent = await client.send(RAW)

    assert (sent.message_id, sent.thread_id) == ("m1", "t1")
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert base64.urlsafe_b64decode(json.loads(request.content)["raw"]) == RAW


async def test_send_refreshes_a_token_it_does_not_have(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(return_value=_token_response())
    respx_mock.post(SEND_URL).mock(
        return_value=httpx.Response(200, json={"id": "m1", "threadId": "t1"})
    )

    await client.send(RAW)

    assert token.call_count == 1


async def test_a_token_failure_inside_send_proves_nothing_was_sent(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=httpx.Response(503))
    send = respx_mock.post(SEND_URL)

    with pytest.raises(GmailUnreachableError) as caught:
        await client.send(RAW)

    assert provably_not_sent(caught.value)
    assert send.call_count == 0


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.PoolTimeout("")]
)
async def test_a_connection_never_made_proves_nothing_was_sent(
    client: HttpGmailClient, respx_mock: respx.MockRouter, error: Exception
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=_token_response())
    respx_mock.post(SEND_URL).mock(side_effect=error)

    with pytest.raises(GmailUnreachableError) as caught:
        await client.send(RAW)

    assert provably_not_sent(caught.value)
    _no_secrets(caught.value)


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        httpx.Response(500),
        httpx.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE"}}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"labelIds": ["SENT"]}),
    ],
    ids=["read-timeout", "disconnected", "500", "503", "200-not-json", "200-no-ids"],
)
async def test_anything_else_leaves_the_outcome_unknown(
    client: HttpGmailClient, respx_mock: respx.MockRouter, outcome: Exception | httpx.Response
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=_token_response())
    if isinstance(outcome, httpx.Response):
        respx_mock.post(SEND_URL).mock(return_value=outcome)
    else:
        respx_mock.post(SEND_URL).mock(side_effect=outcome)

    with pytest.raises(GmailTemporaryError) as caught:
        await client.send(RAW)

    assert not provably_not_sent(caught.value)
    _no_secrets(caught.value)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (400, "invalidArgument"),
        (403, "rateLimitExceeded"),
        (429, "userRateLimitExceeded"),
    ],
)
async def test_a_refusal_is_rejected_and_proves_nothing_was_sent(
    client: HttpGmailClient, respx_mock: respx.MockRouter, status: int, reason: str
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=_token_response())
    respx_mock.post(SEND_URL).mock(return_value=_google_error(status, reason))

    with pytest.raises(GmailRejectedError) as caught:
        await client.send(RAW)

    assert (caught.value.status_code, caught.value.reason) == (status, reason)
    assert provably_not_sent(caught.value)
    _no_secrets(caught.value)


@pytest.mark.parametrize("reason", ["insufficientPermissions", "accessNotConfigured"])
async def test_a_403_about_setup_is_an_authentication_error(
    client: HttpGmailClient, respx_mock: respx.MockRouter, reason: str
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=_token_response())
    respx_mock.post(SEND_URL).mock(return_value=_google_error(403, reason, "PERMISSION_DENIED"))

    with pytest.raises(GmailAuthenticationError):
        await client.send(RAW)


async def test_a_401_drops_the_token_so_the_next_attempt_refreshes(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(return_value=_token_response())
    respx_mock.post(SEND_URL).mock(
        side_effect=[
            _google_error(401, "authError", "UNAUTHENTICATED"),
            httpx.Response(200, json={"id": "m2", "threadId": "t2"}),
        ]
    )

    await client.authorize()
    with pytest.raises(GmailAuthenticationError) as caught:
        await client.send(RAW)
    assert provably_not_sent(caught.value)

    await client.authorize()
    sent = await client.send(RAW)

    assert token.call_count == 2
    assert sent.message_id == "m2"


# --- configuration --------------------------------------------------------------------


# --- reading (M8) -----------------------------------------------------------------------


async def test_can_read_follows_the_tokens_scopes(
    client: HttpGmailClient, clock: Clock, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(TOKEN_URL)
    route.side_effect = [_token_response(), _token_response(scope=BOTH_SCOPES)]

    # A token from before M8: sending still works, reading is off.
    await client.authorize()
    assert await client.can_read() is False

    clock.now += 4000  # the token expired; the new one has both scopes
    assert await client.can_read() is True


async def test_can_read_without_a_scope_list_assumes_so(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3599})
    )

    assert await client.can_read() is True


async def test_search_lists_matching_messages(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=_token_response(scope=BOTH_SCOPES))
    route = respx_mock.get(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "messages": [
                    {"id": "18c1", "threadId": "18c0"},
                    {"id": "18c2", "threadId": "18c2"},
                    {"id": 5},
                ],
                "resultSizeEstimate": 3,
            },
        )
    )

    refs = await client.search('"DoorDash" order', max_results=10)

    assert refs == [GmailRef("18c1", "18c0"), GmailRef("18c2", "18c2")]
    request = route.calls.last.request
    assert request.url.params["q"] == '"DoorDash" order'
    assert request.url.params["maxResults"] == "10"
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


async def test_a_search_with_no_results_is_empty(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=_token_response(scope=BOTH_SCOPES))
    respx_mock.get(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json={"resultSizeEstimate": 0})
    )

    assert await client.search("rfc822msgid:x@y", max_results=1) == []


async def test_get_message_returns_the_full_resource(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=_token_response(scope=BOTH_SCOPES))
    resource = {"id": "18c1", "threadId": "18c0", "payload": {"mimeType": "text/plain"}}
    route = respx_mock.get(f"{MESSAGES_URL}/18c1").mock(
        return_value=httpx.Response(200, json=resource)
    )

    assert await client.get_message("18c1") == resource
    assert route.calls.last.request.url.params["format"] == "full"


async def test_a_message_id_that_is_not_an_id_never_reaches_the_url(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    with pytest.raises(GmailRejectedError):
        await client.get_message("../../drafts?x=1")

    assert respx_mock.calls.call_count == 0


async def test_reading_without_the_scope_is_an_authentication_error(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=_token_response())
    respx_mock.get(MESSAGES_URL).mock(
        return_value=_google_error(403, "insufficientPermissions", "PERMISSION_DENIED")
    )

    with pytest.raises(GmailAuthenticationError, match="insufficientPermissions") as caught:
        await client.search("order", max_results=5)

    _no_secrets(caught.value)


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_a_read_outage_is_temporary(
    client: HttpGmailClient, respx_mock: respx.MockRouter, status: int
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=_token_response(scope=BOTH_SCOPES))
    respx_mock.get(MESSAGES_URL).mock(return_value=httpx.Response(status))

    with pytest.raises(GmailTemporaryError):
        await client.search("order", max_results=5)


async def test_a_read_timeout_is_temporary(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=_token_response(scope=BOTH_SCOPES))
    respx_mock.get(f"{MESSAGES_URL}/18c1").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(GmailTemporaryError) as caught:
        await client.get_message("18c1")

    _no_secrets(caught.value)


async def test_a_deleted_message_is_rejected(
    client: HttpGmailClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=_token_response(scope=BOTH_SCOPES))
    respx_mock.get(f"{MESSAGES_URL}/18c1").mock(
        return_value=_google_error(404, "notFound", "NOT_FOUND")
    )

    with pytest.raises(GmailRejectedError):
        await client.get_message("18c1")


async def test_the_unconfigured_client_cannot_read() -> None:
    gmail = UnconfiguredGmailClient()

    assert await gmail.can_read() is False
    with pytest.raises(GmailAuthenticationError):
        await gmail.search("order", max_results=1)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"google_client_id": "id", "google_client_secret": "secret"},
        {"google_client_id": "id", "google_client_secret": "secret", "gmail_refresh_token": " "},
        {"google_client_id": "", "google_client_secret": "secret", "gmail_refresh_token": "r"},
    ],
)
async def test_missing_credentials_leave_gmail_unconfigured(overrides: dict[str, str]) -> None:
    settings = Settings(
        _env_file=None,
        **{"google_client_id": None, "google_client_secret": None, "gmail_refresh_token": None}
        | overrides,
    )
    async with httpx.AsyncClient() as http:
        gmail = build_gmail_client(settings, http)

    assert isinstance(gmail, UnconfiguredGmailClient)
    with pytest.raises(GmailAuthenticationError):
        await gmail.authorize()


async def test_full_credentials_build_the_real_client() -> None:
    settings = Settings(
        _env_file=None,
        google_client_id="id",
        google_client_secret=SecretStr("secret"),
        gmail_refresh_token=SecretStr("refresh"),
    )
    async with httpx.AsyncClient() as http:
        assert isinstance(build_gmail_client(settings, http), HttpGmailClient)

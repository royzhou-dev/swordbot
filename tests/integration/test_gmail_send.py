"""The Send flow with the real Gmail client on mocked Google endpoints (M7 part 2).

`test_sending_flow.py` covers the D14 outcomes with a fake Gmail. These check
that `HttpGmailClient`'s answers land on the same outcomes: Gmail's ids are
stored, a refusal offers the email again, and a server error asks the user.
"""

import json
from base64 import urlsafe_b64decode
from collections.abc import AsyncIterator
from email import message_from_bytes
from email.policy import default

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.models import CaseStatus
from app.db.session import Database
from app.email.gmail_client import GMAIL_SEND_SCOPE, HttpGmailClient
from app.email.models import OutboundEmailStatus
from app.email.sending import SENT_REPLY
from app.users.models import User
from tests.integration.harness import SUBJECT, SUPPORT, Harness

S = OutboundEmailStatus
TOKEN_URL = "https://oauth.test/token"
SEND_URL = "https://gmail.test/gmail/v1/users/me/messages/send"


@pytest.fixture
async def gmail() -> AsyncIterator[HttpGmailClient]:
    async with httpx.AsyncClient() as http:
        yield HttpGmailClient(
            client_id="client-id",
            client_secret=SecretStr("client-secret"),
            refresh_token=SecretStr("refresh-token"),
            http=http,
            token_url=TOKEN_URL,
            api_base_url="https://gmail.test",
        )


@pytest.fixture
def google(respx_mock: respx.MockRouter) -> respx.MockRouter:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "ya29.token", "expires_in": 3599, "scope": GMAIL_SEND_SCOPE}
        )
    )
    return respx_mock


async def _approve(h: Harness) -> None:
    await h.reach_draft()
    await h.press(h.buttons()["Send"])


async def test_an_accepted_send_stores_gmails_ids(
    database: Database,
    session: AsyncSession,
    user: User,
    gmail: HttpGmailClient,
    google: respx.MockRouter,
) -> None:
    send = google.post(SEND_URL).mock(
        return_value=httpx.Response(200, json={"id": "18f0a1", "threadId": "18f0a0"})
    )
    h = Harness(database, session, gmail=gmail)

    await _approve(h)

    assert send.call_count == 1
    raw = urlsafe_b64decode(json.loads(send.calls.last.request.content)["raw"])
    message = message_from_bytes(raw, policy=default)
    assert (message["To"], message["Subject"]) == (SUPPORT, SUBJECT)
    [email] = await h.emails()
    assert email.status is S.SENT
    assert (email.gmail_message_id, email.gmail_thread_id) == ("18f0a1", "18f0a0")
    case = await h.only_case()
    assert case.status is CaseStatus.WAITING_FOR_SUPPORT
    assert case.gmail_thread_id == "18f0a0"
    assert h.telegram.sent_texts[-1] == SENT_REPLY.format(to=SUPPORT, subject=SUBJECT)


async def test_a_refused_send_is_offered_again(
    database: Database,
    session: AsyncSession,
    user: User,
    gmail: HttpGmailClient,
    google: respx.MockRouter,
) -> None:
    send = google.post(SEND_URL).mock(
        return_value=httpx.Response(
            400, json={"error": {"code": 400, "errors": [{"reason": "invalidArgument"}]}}
        )
    )
    h = Harness(database, session, gmail=gmail)

    await _approve(h)

    assert send.call_count == 1
    assert [e.status for e in await h.emails()] == [S.FAILED, S.AWAITING_APPROVAL]
    assert (await h.only_case()).status is CaseStatus.WAITING_FOR_USER_APPROVAL
    assert "Send" in h.buttons()


async def test_a_server_error_asks_the_user_and_never_resends(
    database: Database,
    session: AsyncSession,
    user: User,
    gmail: HttpGmailClient,
    google: respx.MockRouter,
) -> None:
    send = google.post(SEND_URL).mock(return_value=httpx.Response(500))
    h = Harness(database, session, gmail=gmail)

    await _approve(h)

    assert send.call_count == 1
    assert [e.status for e in await h.emails()] == [S.NEEDS_ATTENTION]
    assert (await h.only_case()).status is CaseStatus.READY_TO_SEND
    assert set(h.buttons()) == {"It was sent", "It wasn't sent"}

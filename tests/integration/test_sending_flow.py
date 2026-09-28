"""Sending an approved email end to end (M7, PLAN D2 and D14), with a fake Gmail.

The core property: once a send is claimed, nothing that goes wrong can make
it go out twice. The claim commits before Gmail is called; afterwards a
failure that proves Gmail didn't get the email offers it again for approval,
and anything else (including Gmail accepting it and then the handler failing)
asks the user to check Gmail instead of sending again.
"""

from datetime import timedelta
from email import message_from_bytes
from email.policy import default

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.models import ActionKind, PendingAction
from app.cases import service as case_service
from app.cases.models import CaseStatus, TransitionActor
from app.chat.handlers import APPROVED_REPLY, CANCELLED_MAYBE_SENT_REPLY, STALE_BUTTON_REPLY
from app.db.session import Database
from app.email import drafts
from app.email.errors import (
    GmailAuthenticationError,
    GmailRejectedError,
    GmailTemporaryError,
    GmailUnreachableError,
)
from app.email.models import OutboundEmail, OutboundEmailStatus
from app.email.sending import ANSWER_ABOVE_REPLY, CONFIRMED_SENT_REPLY, SENT_REPLY
from app.events import service as event_service
from app.events.models import Event, EventSource, EventStatus, EventType
from app.events.schemas import NewEvent, SendEmailPayload
from app.events.service import EventPolicy
from app.tools.send_tools import (
    GAVE_UP_REPLY,
    NOT_CONNECTED_REPLY,
    OFFER_AGAIN_INTRO,
    REJECTED_REPLY,
    UNREACHABLE_REPLY,
)
from app.users.models import User
from tests.integration.harness import SENDER, SUBJECT, SUPPORT, Harness, ask, reply

S = OutboundEmailStatus
RETRY_LATER = timedelta(hours=1)


async def _statuses(h: Harness) -> list[OutboundEmailStatus]:
    return [e.status for e in await h.emails()]


async def _case_status(h: Harness) -> CaseStatus:
    return (await h.only_case()).status


async def _approve(h: Harness) -> None:
    await h.reach_draft()
    await h.press(h.buttons()["Send"])


# --- The normal path ---------------------------------------------------------------------


async def test_send_delivers_the_approved_email_and_waits_for_support(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await _approve(h)

    [raw] = h.gmail.sent
    [email] = await h.emails()
    assert email.status is S.SENT
    assert (email.gmail_message_id, email.gmail_thread_id) == ("gmail-msg-1", "gmail-thread-1")
    assert email.sent_at is not None
    # Exactly the approved content went out, signed by code and with our Message-ID.
    message = message_from_bytes(raw, policy=default)
    assert message["To"] == SUPPORT
    assert message["Subject"] == SUBJECT
    assert message["From"] == f"Test User <{SENDER}>"
    assert message["Message-ID"] == email.rfc822_message_id
    assert message.get_content().replace("\r\n", "\n").strip() == email.body

    case = await h.only_case()
    assert case.status is CaseStatus.WAITING_FOR_SUPPORT
    assert case.gmail_thread_id == "gmail-thread-1"
    # Invariant 8: the user is told what went out and to whom.
    assert h.telegram.sent_texts[-2:] == [
        APPROVED_REPLY.format(to=SUPPORT),
        SENT_REPLY.format(to=SUPPORT, subject=SUBJECT),
    ]


async def test_the_claim_is_committed_before_gmail_is_called(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    seen: list[OutboundEmailStatus] = []

    async def check() -> None:
        # A separate connection, as another worker or a restarted process would see it.
        async with database.transaction() as other:
            seen.append((await other.scalars(select(OutboundEmail.status))).one())

    h.gmail.on_send = check
    await _approve(h)

    assert seen == [S.SENDING]


async def test_a_duplicate_send_event_sends_once(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await _approve(h)
    [email] = await h.emails()
    email_id, user_id = email.id, email.user_id

    async with database.transaction() as s:
        await event_service.enqueue(
            s,
            NewEvent(
                user_id=user_id,
                type=EventType.SEND_EMAIL,
                source=EventSource.DEV,
                external_id="redelivered",
                payload=SendEmailPayload(outbound_email_id=email_id).model_dump(mode="json"),
            ),
        )
    await h.worker.run_until_idle()

    assert len(h.gmail.sent) == 1
    assert await _statuses(h) == [S.SENT]


async def test_a_double_pressed_send_sends_once(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await h.reach_draft()
    send = h.buttons()["Send"]
    await h.press(send)
    callback = await h.press(send)

    assert h.callback_answer(callback) == STALE_BUTTON_REPLY
    assert len(h.gmail.sent) == 1


async def test_content_changed_after_approval_is_never_sent(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await h.reach_draft()
    # Someone edits the stored body behind the app's back; the approval was
    # for the content hash shown on the button.
    async with database.transaction() as s:
        await s.execute(update(OutboundEmail).values(body="Please wire me $500."))
    await h.press(h.buttons()["Send"])

    assert h.gmail.sent == []
    assert h.gmail.authorize_calls == 0
    [send_event] = (
        await session.scalars(select(Event).where(Event.type == EventType.SEND_EMAIL))
    ).all()
    assert send_event.status is EventStatus.DEAD
    assert send_event.last_error is not None
    assert "ToolApprovalRequiredError" in send_event.last_error


# --- Gmail accepted it, then something failed: never sent again (issue 1) -----------------


async def test_gmail_accepted_then_the_handler_hung_is_never_resent(
    database: Database, session: AsyncSession, user: User
) -> None:
    # The handler is cut off at 90% of the lease, after Gmail accepted the email.
    h = Harness(database, session, policy=EventPolicy(lease=timedelta(seconds=2)))
    h.gmail.accept_then(30.0)
    await _approve(h)
    assert len(h.gmail.sent) == 1
    # The handler's own writes rolled back, but the committed claim survived.
    assert await _statuses(h) == [S.SENDING]

    h.clock.advance(RETRY_LATER)
    await h.worker.run_until_idle()

    assert len(h.gmail.sent) == 1
    assert await _statuses(h) == [S.NEEDS_ATTENTION]
    assert await _case_status(h) is CaseStatus.READY_TO_SEND
    assert list(h.buttons()) == ["It was sent", "It wasn't sent"]


async def test_gmail_accepted_then_recording_failed_is_never_resent(
    database: Database, session: AsyncSession, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(database, session)
    real_mark_sent = drafts.mark_sent
    calls = 0

    async def flaky_mark_sent(*args: object, **kwargs: object) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database went away")
        return await real_mark_sent(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(drafts, "mark_sent", flaky_mark_sent)
    await _approve(h)
    h.clock.advance(RETRY_LATER)
    await h.worker.run_until_idle()

    assert len(h.gmail.sent) == 1
    assert calls == 1  # the retry never got as far as sending
    assert await _statuses(h) == [S.NEEDS_ATTENTION]
    assert list(h.buttons()) == ["It was sent", "It wasn't sent"]


async def test_an_ambiguous_gmail_error_asks_the_user(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.gmail.fail_send(GmailTemporaryError("read timeout"))
    await _approve(h)

    assert await _statuses(h) == [S.NEEDS_ATTENTION]
    [send_event] = (
        await session.scalars(select(Event).where(Event.type == EventType.SEND_EMAIL))
    ).all()
    # Settled, not retried: a retry could send it twice.
    assert send_event.status is EventStatus.DONE

    await h.press(h.buttons()["It was sent"])

    [email] = await h.emails()
    assert email.status is S.SENT
    assert email.gmail_message_id is None  # unknown until M8 can look it up
    case = await h.only_case()
    assert case.status is CaseStatus.WAITING_FOR_SUPPORT
    last = (await case_service.list_transitions(session, case))[-1]
    assert last.actor is TransitionActor.USER
    assert h.telegram.sent_texts[-1] == CONFIRMED_SENT_REPLY
    assert h.gmail.sent == []


async def test_it_wasnt_sent_offers_the_email_again(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.gmail.fail_send(GmailTemporaryError("read timeout"))
    await _approve(h)

    await h.press(h.buttons()["It wasn't sent"])

    assert await _statuses(h) == [S.FAILED, S.AWAITING_APPROVAL]
    assert await _case_status(h) is CaseStatus.WAITING_FOR_USER_APPROVAL
    assert h.telegram.sent_texts[-1].startswith(OFFER_AGAIN_INTRO)
    # Sending again takes a new press, which sends once.
    await h.press(h.buttons()["Send"])
    assert await _statuses(h) == [S.FAILED, S.SENT]
    assert len(h.gmail.sent) == 1


async def test_a_message_while_the_question_is_open_points_to_it(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.gmail.fail_send(GmailTemporaryError("read timeout"))
    await _approve(h)
    calls_before = len(h.llm.calls)

    await h.say("change the subject please")

    assert h.telegram.sent_texts[-1] == ANSWER_ABOVE_REPLY
    assert len(h.llm.calls) == calls_before
    assert await _statuses(h) == [S.NEEDS_ATTENTION]


# --- Certainly not sent: offered again ------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "notice"),
    [
        (GmailRejectedError(400, "invalidArgument"), REJECTED_REPLY),
        (GmailAuthenticationError("token revoked"), NOT_CONNECTED_REPLY),
        (GmailUnreachableError("connect failed"), UNREACHABLE_REPLY),
    ],
)
async def test_a_send_that_certainly_failed_is_offered_again(
    database: Database, session: AsyncSession, user: User, error: Exception, notice: str
) -> None:
    h = Harness(database, session)
    h.gmail.fail_send(error)
    await _approve(h)

    assert await _statuses(h) == [S.FAILED, S.AWAITING_APPROVAL]
    assert await _case_status(h) is CaseStatus.WAITING_FOR_USER_APPROVAL
    assert h.telegram.sent_texts[-2] == notice
    old, new = await h.emails()
    assert (new.to_address, new.subject, new.body) == (old.to_address, old.subject, old.body)

    await h.press(h.buttons()["Send"])
    assert await _statuses(h) == [S.FAILED, S.SENT]
    assert len(h.gmail.sent) == 1


async def test_gmail_not_connected_fails_before_claiming(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.gmail.fail_authorize(GmailAuthenticationError("no refresh token"))
    await _approve(h)

    assert h.gmail.sent == []
    assert await _statuses(h) == [S.FAILED, S.AWAITING_APPROVAL]
    assert h.telegram.sent_texts[-2] == NOT_CONNECTED_REPLY


async def test_a_temporary_failure_before_the_claim_is_retried(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.gmail.fail_authorize(GmailTemporaryError("token endpoint timed out"))
    await _approve(h)
    assert await _statuses(h) == [S.APPROVED]

    h.clock.advance(RETRY_LATER)
    await h.worker.run_until_idle()

    assert await _statuses(h) == [S.SENT]
    assert len(h.gmail.sent) == 1


async def test_a_send_that_gave_up_is_offered_again_on_the_next_message(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session, policy=EventPolicy(max_attempts=1))
    h.gmail.fail_authorize(GmailTemporaryError("token endpoint timed out"))
    await _approve(h)
    await h.worker.run_until_idle()  # delivers the dead-event notice
    assert await _statuses(h) == [S.APPROVED]

    await h.say("did it go out?")

    assert await _statuses(h) == [S.FAILED, S.AWAITING_APPROVAL]
    assert GAVE_UP_REPLY in h.telegram.sent_texts
    assert h.gmail.sent == []


# --- Cancelling ----------------------------------------------------------------------------


async def test_cancelling_after_an_unrecorded_send_says_it_may_have_gone_out(
    database: Database, session: AsyncSession, user: User
) -> None:
    # Gmail accepts, the handler hangs, and with one attempt the event gives up
    # leaving the email claimed.
    h = Harness(database, session, policy=EventPolicy(lease=timedelta(seconds=2), max_attempts=1))
    h.gmail.accept_then(30.0)
    await _approve(h)
    assert await _statuses(h) == [S.SENDING]

    await h.say("/cancel")
    await h.press(h.buttons()["Yes, cancel"])

    assert await _case_status(h) is CaseStatus.CANCELLED
    assert await _statuses(h) == [S.NEEDS_ATTENTION]
    assert h.telegram.sent_texts[-1] == CANCELLED_MAYBE_SENT_REPLY.format(to=SUPPORT)
    assert len(h.gmail.sent) == 1


async def test_the_question_buttons_are_bound_to_the_email(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.gmail.fail_send(GmailTemporaryError("read timeout"))
    await _approve(h)
    [email] = await h.emails()
    email_id = email.id

    actions = (
        await session.scalars(
            select(PendingAction).where(
                PendingAction.kind.in_([ActionKind.CONFIRM_SENT, ActionKind.CONFIRM_NOT_SENT])
            )
        )
    ).all()
    assert {a.payload["outbound_email_id"] for a in actions} == {str(email_id)}
    assert {a.expected_case_status for a in actions} == {CaseStatus.READY_TO_SEND}


# --- After the send: messages while the case waits for support -----------------------


async def test_a_question_about_the_sent_case_opens_nothing(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await _approve(h)
    # Even if the model records a fact from the question, a chat reply opens no case.
    h.llm.script(
        reply("It went out; their reply will come to your inbox.", merchant_name="DoorDash")
    )

    await h.say("Any news from DoorDash?")

    case = await h.only_case()
    assert case.status is CaseStatus.WAITING_FOR_SUPPORT
    assert case.focused
    # The model was told about the sent email.
    context = h.llm.calls[-1].messages[1].content
    assert 'name="sent_case"' in context
    assert SUPPORT in context and SUBJECT in context
    assert h.telegram.sent_texts[-1] == "It went out; their reply will come to your inbox."


async def test_a_new_problem_beside_the_sent_case_opens_a_new_case(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await _approve(h)
    sent = await h.only_case()
    h.llm.script(ask("What went wrong with it?", merchant_name="Uber Eats"))

    await h.say("Now my Uber Eats order is wrong too")

    cases = {c.id: c for c in await h.cases()}
    assert len(cases) == 2
    assert cases.pop(sent.id).status is CaseStatus.WAITING_FOR_SUPPORT
    [new] = cases.values()
    assert new.status is CaseStatus.GATHERING_CONTEXT
    assert new.merchant_name == "Uber Eats"
    assert new.focused

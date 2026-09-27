"""Drafting and approval end to end (M6): draft -> [Send] [Edit] [Cancel].

The PLAN's M6 checks: text like "yeah looks good" never approves anything
(only the Send button does), an edit makes the old Send button stop working,
and a double-pressed Send approves once. Asserts on states and records, never
on the model's wording.
"""

from typing import get_args

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.models import ActionKind, ActionStatus, PendingAction
from app.agent.drafting import request_draft
from app.agent.policies import FALLBACK_QUESTIONS, Requirement
from app.cases import service as case_service
from app.cases.models import CaseStatus, TransitionActor
from app.chat.handlers import (
    APPROVED_REPLY,
    APPROVED_WAITING_REPLY,
    CANCELLED_REPLY,
    EDIT_PROMPT,
    KEPT_REPLY,
    STALE_BUTTON_REPLY,
)
from app.db.session import Database
from app.email.drafts import SIGN_OFF, content_hash
from app.email.models import OutboundEmailStatus
from app.events.models import Event, EventStatus, EventType
from app.tools.chat_tools import ReplyToUser
from app.tools.email_tools import DRAFT_INTRO, REPEAT_INTRO, REVISED_INTRO, DraftSupportEmail
from app.users.models import User
from tests.integration.harness import (
    BODY,
    SUBJECT,
    SUPPORT,
    Harness,
    email,
    finish,
    reply,
    revise,
)

S = OutboundEmailStatus


async def test_the_draft_is_shown_with_its_recipient_and_signature(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    case_id = (await h.reach_draft()).id

    [draft] = await h.emails()
    assert draft.version == 1
    assert draft.status is S.AWAITING_APPROVAL
    assert draft.to_address == SUPPORT
    assert draft.subject == SUBJECT
    assert draft.body_text == BODY
    # Signed by code with the user's Telegram name.
    assert draft.signature_name == "Test User"
    assert draft.body == f"{BODY}\n\n{SIGN_OFF}\nTest User"
    assert draft.content_hash == content_hash(SUPPORT, SUBJECT, draft.body)
    assert draft.case_id == case_id

    shown = h.telegram.sent_texts[-1]
    assert shown.startswith(DRAFT_INTRO)
    assert f"To: {SUPPORT}" in shown and f"Subject: {SUBJECT}" in shown and draft.body in shown
    assert list(h.buttons()) == ["Send", "Edit", "Cancel"]
    actions = (await session.scalars(select(PendingAction))).all()
    assert {a.group_id for a in actions} == {draft.action_group_id}
    assert all(a.expected_case_status is CaseStatus.WAITING_FOR_USER_APPROVAL for a in actions)


async def test_send_approves_exactly_the_shown_draft(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    case = await h.reach_draft()
    send = h.buttons()["Send"]

    callback = await h.press(send)

    assert h.callback_answer(callback) is None
    [draft] = await h.emails()
    assert draft.status is S.APPROVED
    send_action = (
        await session.scalars(
            select(PendingAction).where(PendingAction.kind == ActionKind.SEND_EMAIL)
        )
    ).one()
    assert draft.approved_by_action_id == send_action.id
    assert send_action.status is ActionStatus.CONSUMED
    assert draft.approved_by_event_id == send_action.consumed_by_event_id

    case = await h.only_case()
    assert case.status is CaseStatus.READY_TO_SEND
    last = (await case_service.list_transitions(session, case))[-1]
    assert (last.from_status, last.to_status, last.actor) == (
        CaseStatus.WAITING_FOR_USER_APPROVAL,
        CaseStatus.READY_TO_SEND,
        TransitionActor.USER,
    )
    assert h.telegram.sent_texts[-1] == APPROVED_REPLY.format(to=SUPPORT)


async def test_words_never_approve(database: Database, session: AsyncSession, user: User) -> None:
    h = Harness(database, session)
    await h.reach_draft()
    sent_before = len(h.telegram.sent_texts)

    h.llm.script(reply("Great! Tap Send under the email when you're ready."))
    await h.say("yeah looks good, send it")

    [draft] = await h.emails()
    assert draft.status is S.AWAITING_APPROVAL
    assert draft.approved_at is None
    assert (await h.only_case()).status is CaseStatus.WAITING_FOR_USER_APPROVAL
    # Only the reply was sent; the draft's buttons were still open, so it isn't repeated.
    assert len(h.telegram.sent_texts) == sent_before + 1
    # The review step can only draft or reply: approval isn't among its actions.
    [review_call] = [c for c in h.llm.calls if c.purpose == "draft_review"]
    assert review_call.schema is not None
    action = review_call.schema.model_fields["action"].annotation
    assert get_args(action) == (DraftSupportEmail, ReplyToUser)

    # The original Send button still works.
    await h.press(h.buttons()["Send"])
    assert (await h.emails())[0].status is S.APPROVED


async def test_an_edit_makes_the_old_send_button_stop_working(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await h.reach_draft()
    old = h.buttons()

    await h.press(old["Edit"])
    assert h.telegram.sent_texts[-1] == EDIT_PROMPT

    shorter = "Hi,\n\nMy order tonight was missing the fries. Please refund them."
    h.llm.script(revise(body=shorter))
    await h.say("make it shorter")

    v1, v2 = await h.emails()
    assert (v1.status, v2.status) == (S.SUPERSEDED, S.AWAITING_APPROVAL)
    assert v2.supersedes_id == v1.id and v2.body_text == shorter
    assert v2.content_hash != v1.content_hash
    assert h.telegram.sent_texts[-1].startswith(REVISED_INTRO)
    new = h.buttons()
    assert new["Send"] != old["Send"]

    # The old Send button approves nothing.
    callback = await h.press(old["Send"])
    assert h.callback_answer(callback) == STALE_BUTTON_REPLY
    assert [e.status for e in await h.emails()] == [S.SUPERSEDED, S.AWAITING_APPROVAL]

    await h.press(new["Send"])
    v1, v2 = await h.emails()
    assert (v1.status, v2.status) == (S.SUPERSEDED, S.APPROVED)


async def test_a_change_typed_without_pressing_edit_retires_the_old_buttons(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await h.reach_draft()
    old = h.buttons()
    old_send = (
        await session.scalars(
            select(PendingAction).where(PendingAction.kind == ActionKind.SEND_EMAIL)
        )
    ).one()

    h.llm.script(revise(body=BODY + " They were also cold."))
    await h.say("mention everything was cold too")

    # The old message's buttons are removed in the chat, and closed in the database.
    assert {"chat_id": 1_000_000_001, "message_id": old_send.telegram_message_id} in [
        {k: c[k] for k in ("chat_id", "message_id")}
        for c in h.telegram.calls_to("edit_message_reply_markup")
    ]
    await session.refresh(old_send)
    assert old_send.status is ActionStatus.SUPERSEDED

    callback = await h.press(old["Send"])
    assert h.callback_answer(callback) == STALE_BUTTON_REPLY
    assert [e.status for e in await h.emails()] == [S.SUPERSEDED, S.AWAITING_APPROVAL]


async def test_a_double_pressed_send_approves_once(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await h.reach_draft()
    send = h.buttons()["Send"]

    first = await h.press(send)
    second = await h.press(send)

    assert h.callback_answer(first) is None
    assert h.callback_answer(second) == STALE_BUTTON_REPLY
    [draft] = await h.emails()
    assert draft.status is S.APPROVED
    case = await h.only_case()
    to_ready = [
        t
        for t in await case_service.list_transitions(session, case)
        if t.to_status is CaseStatus.READY_TO_SEND
    ]
    assert len(to_ready) == 1
    assert h.telegram.sent_texts.count(APPROVED_REPLY.format(to=SUPPORT)) == 1


async def test_edit_then_never_mind_shows_the_draft_again(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await h.reach_draft()
    await h.press(h.buttons()["Edit"])

    h.llm.script(reply("No problem."))
    await h.say("never mind, it's fine")

    assert h.telegram.sent_texts[-2] == "No problem."
    assert h.telegram.sent_texts[-1].startswith(REPEAT_INTRO)
    await h.press(h.buttons()["Send"])
    [draft] = await h.emails()
    assert draft.status is S.APPROVED
    assert draft.version == 1


async def test_cancel_under_the_draft_asks_first(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await h.reach_draft()
    await h.press(h.buttons()["Cancel"])
    assert list(h.buttons()) == ["Yes, cancel", "Keep it"]

    # Keep it: the draft comes back with working buttons.
    await h.press(h.buttons()["Keep it"])
    assert h.telegram.sent_texts[-2] == KEPT_REPLY
    assert h.telegram.sent_texts[-1].startswith(REPEAT_INTRO)
    assert (await h.only_case()).status is CaseStatus.WAITING_FOR_USER_APPROVAL

    await h.press(h.buttons()["Cancel"])
    await h.press(h.buttons()["Yes, cancel"])

    assert (await h.only_case()).status is CaseStatus.CANCELLED
    [draft] = await h.emails()
    assert draft.status is S.CANCELLED
    assert h.telegram.sent_texts[-1] == CANCELLED_REPLY.format(merchant="DoorDash support")


async def test_slash_cancel_during_approval_cancels_the_draft(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await h.reach_draft()
    draft_send = h.buttons()["Send"]

    await h.say("/cancel")
    await h.press(h.buttons()["Yes, cancel"])

    [draft] = await h.emails()
    assert draft.status is S.CANCELLED
    # The draft's still-open buttons were closed too.
    callback = await h.press(draft_send)
    assert h.callback_answer(callback) == STALE_BUTTON_REPLY
    assert (await h.emails())[0].status is S.CANCELLED


async def test_a_new_address_or_name_makes_a_new_version(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await h.reach_draft()

    # The model only replies, but the facts changed: code re-issues the draft.
    h.llm.script(reply("Done.", support_email="help@doordash.com"))
    await h.say("actually send it to help@doordash.com")
    h.llm.script(reply("Sure.", signature_name="Alex Kim"))
    await h.say("the order is under Alex Kim, sign it with that")

    v1, v2, v3 = await h.emails()
    assert [e.status for e in (v1, v2, v3)] == [S.SUPERSEDED, S.SUPERSEDED, S.AWAITING_APPROVAL]
    assert v2.to_address == "help@doordash.com" and v2.body_text == BODY
    assert v3.signature_name == "Alex Kim"
    assert v3.body.endswith(f"{SIGN_OFF}\nAlex Kim")
    assert (await h.only_case()).support_email == "help@doordash.com"


async def test_a_change_needing_more_details_goes_back_to_intake(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await h.reach_draft()

    h.llm.script(reply("Oh no.", issue_type="damaged_item"))
    await h.say("wait, they weren't missing, the fries were crushed")

    case = await h.only_case()
    assert case.status is CaseStatus.GATHERING_CONTEXT
    [draft] = await h.emails()
    assert draft.status is S.SUPERSEDED
    assert h.telegram.sent_texts[-1] == FALLBACK_QUESTIONS[Requirement.AFFECTED_ITEMS]

    # Answering finishes intake again and drafts version 2.
    h.llm.script(finish(affected_items="fries"), email())
    await h.say("the fries")

    case = await h.only_case()
    assert case.status is CaseStatus.WAITING_FOR_USER_APPROVAL
    v1, v2 = await h.emails()
    assert (v1.status, v2.status) == (S.SUPERSEDED, S.AWAITING_APPROVAL)


async def test_an_approved_email_cant_be_changed_from_chat(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    await h.reach_draft()
    await h.press(h.buttons()["Send"])
    calls_before = len(h.llm.calls)

    await h.say("change the subject please")

    assert h.telegram.sent_texts[-1] == APPROVED_WAITING_REPLY
    assert len(h.llm.calls) == calls_before
    assert (await h.emails())[0].status is S.APPROVED


async def test_duplicate_draft_events_draft_once(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    case = await h.reach_draft()
    case_id, user_id = case.id, case.user_id
    # Two more draft requests (e.g. redelivered): the case is past drafting.
    async with database.transaction() as s:
        loaded = await case_service.get_case(s, case_id, user_id=user_id)
        for cause in (1, 2):
            await request_draft(s, loaded, cause_event_id=10_000 + cause, now=h.clock())
    await h.worker.run_until_idle()

    assert len(await h.emails()) == 1
    assert h.llm.unused_replies == 0
    drafts = (await session.scalars(select(Event).where(Event.type == EventType.DRAFT_EMAIL))).all()
    assert [e.status for e in drafts] == [EventStatus.DONE] * 3

"""The intake conversation end to end: Telegram update -> worker -> fake LLM -> case.

Asserts on actions, states and recorded facts, never on the model's wording.
Runs against a real database (SQLite, and Postgres in CI) and the real worker.
What happens to the draft afterwards is in test_drafting_flow.py.
"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.intake import NO_CASE_REPLY, drafting_notice
from app.agent.policies import FALLBACK_QUESTIONS, Requirement
from app.cases import service as case_service
from app.cases.models import (
    CaseMessage,
    CaseStatus,
    FactSource,
    MessageRole,
    TransitionActor,
)
from app.db.session import Database
from app.events.models import Event, EventStatus, EventType
from app.llm.errors import LLMTemporaryError
from app.users.models import User
from tests.integration.harness import (
    ANSWER,
    COMPLAINT,
    QUESTION,
    SUPPORT,
    Harness,
    ask,
    complaint_facts,
    email,
    finish,
    reply,
)


async def test_complaint_question_answer_reaches_a_draft(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    today = h.today
    h.llm.script(ask(**complaint_facts(today)))
    first = await h.say(COMPLAINT)

    case = await h.only_case()
    assert case.status is CaseStatus.GATHERING_CONTEXT
    assert case.focused
    assert h.telegram.sent_texts == [QUESTION]

    h.llm.script(finish(desired_resolution="refund", support_email=SUPPORT), email())
    second = await h.say(ANSWER)

    case = await h.only_case()
    assert case.status is CaseStatus.WAITING_FOR_USER_APPROVAL
    assert case.merchant_name == "DoorDash"
    assert case.issue_type == "missing_item"
    assert case.order_date is not None and case.order_date.isoformat() == today
    assert case.desired_resolution == "refund"
    assert case.support_email == SUPPORT

    facts = await h.facts(case)
    assert all(f.source is FactSource.USER_MESSAGE for f in facts)
    assert {f.key: f.source_ref for f in facts} == {
        "merchant_name": f"telegram:{first}",
        "issue_type": f"telegram:{first}",
        "issue_summary": f"telegram:{first}",
        "order_date": f"telegram:{first}",
        "missing_items": f"telegram:{first}",
        "desired_resolution": f"telegram:{second}",
        "support_email": f"telegram:{second}",
    }

    events = (await session.scalars(select(Event).order_by(Event.id))).all()
    messages_in = [e for e in events if e.type is EventType.USER_MESSAGE]
    [draft_event] = [e for e in events if e.type is EventType.DRAFT_EMAIL]
    assert draft_event.status is EventStatus.DONE
    assert draft_event.case_id == case.id
    transitions = await case_service.list_transitions(session, case)
    assert [(t.from_status, t.to_status, t.actor, t.event_id) for t in transitions] == [
        (None, CaseStatus.GATHERING_CONTEXT, TransitionActor.USER, messages_in[0].id),
        (
            CaseStatus.GATHERING_CONTEXT,
            CaseStatus.READY_TO_DRAFT,
            TransitionActor.SYSTEM,
            messages_in[1].id,
        ),
        (
            CaseStatus.READY_TO_DRAFT,
            CaseStatus.WAITING_FOR_USER_APPROVAL,
            TransitionActor.SYSTEM,
            draft_event.id,
        ),
    ]

    # The question, the drafting notice, then the draft with its buttons.
    assert h.telegram.sent_texts[:2] == [QUESTION, drafting_notice(case)]
    assert list(h.buttons()) == ["Send", "Edit", "Cancel"]

    messages = (
        await session.scalars(
            select(CaseMessage).where(CaseMessage.case_id == case.id).order_by(CaseMessage.id)
        )
    ).all()
    assert [(m.role, m.telegram_message_id) for m in messages] == [
        (MessageRole.USER, first),
        (MessageRole.ASSISTANT, None),
        (MessageRole.USER, second),
        (MessageRole.ASSISTANT, None),
        (MessageRole.ASSISTANT, None),
    ]

    # The second step saw the conversation so far, from the database.
    second_call = h.llm.calls[1].messages
    assert [m.role for m in second_call] == ["system", "system", "user", "assistant", "user"]
    assert second_call[2].content == COMPLAINT
    assert second_call[3].content == QUESTION
    # The drafter got the case facts, not the chat.
    draft_call = h.llm.calls[2]
    assert draft_call.purpose == "draft_email"
    assert SUPPORT in draft_call.messages[1].content
    assert COMPLAINT not in "".join(m.content for m in draft_call.messages)
    assert h.llm.unused_replies == 0


async def test_small_talk_opens_no_case(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.llm.script(reply("Hello!"), finish())
    await h.say("hi")
    await h.say("how are you")

    assert await h.cases() == []
    assert h.telegram.sent_texts == ["Hello!", NO_CASE_REPLY]


async def test_the_model_finishing_early_gets_a_fallback_question(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.llm.script(finish(merchant_name="Amazon", issue_type="damaged_item"))
    await h.say("My Amazon package showed up damaged")

    case = await h.only_case()
    assert case.status is CaseStatus.GATHERING_CONTEXT
    assert h.telegram.sent_texts == [FALLBACK_QUESTIONS[Requirement.ISSUE_SUMMARY]]


async def test_the_support_address_is_asked_for_last(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.llm.script(ask(**complaint_facts(h.today)), finish(desired_resolution="refund"))
    await h.say(COMPLAINT)
    await h.say("a refund please")

    case = await h.only_case()
    assert case.status is CaseStatus.GATHERING_CONTEXT
    assert h.telegram.sent_texts[-1] == FALLBACK_QUESTIONS[Requirement.SUPPORT_EMAIL]


async def test_an_order_number_the_user_never_typed_is_not_recorded(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.llm.script(ask(merchant_name="DoorDash", order_number="123-456"))
    await h.say("DoorDash forgot my fries")

    assert "order_number" not in await h.current(await h.only_case())


async def test_a_support_address_the_user_never_typed_is_not_recorded(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.llm.script(ask(merchant_name="DoorDash", support_email="support@doordash.com"))
    await h.say("DoorDash forgot my fries, I don't know their email")

    assert "support_email" not in await h.current(await h.only_case())


async def test_a_failed_draft_is_retried_by_the_next_message(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    # The drafter's output has a placeholder, even after the repair retry.
    h.llm.script(
        ask(**complaint_facts(h.today)),
        finish(desired_resolution="refund", support_email=SUPPORT),
        email(body="Hi,\n\nMy order was missing the fries.\n\n[Your Name]"),
    )
    await h.say(COMPLAINT)
    await h.say(ANSWER)
    await h.worker.run_until_idle()  # delivers the failure notice

    case = await h.only_case()
    assert case.status is CaseStatus.READY_TO_DRAFT
    assert await h.emails() == []
    assert "went wrong" in h.telegram.sent_texts[-1]

    # Anything the user says next queues the draft again.
    h.llm.script(reply("Sorry about that, trying again."), email())
    await h.say("hello?")

    case = await h.only_case()
    assert case.status is CaseStatus.WAITING_FOR_USER_APPROVAL
    assert len(await h.emails()) == 1
    assert h.llm.unused_replies == 0


async def test_a_new_requirement_sends_the_case_back_to_gathering(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.llm.script(
        ask(**complaint_facts(h.today)),
        # Changes the issue type in the same message that completes intake.
        ask("Which items were damaged?", desired_resolution="refund", issue_type="damaged_item"),
    )
    await h.say(COMPLAINT)
    await h.say("refund. oh and they weren't missing, the fries were crushed")

    case = await h.only_case()
    assert case.status is CaseStatus.GATHERING_CONTEXT
    assert h.telegram.sent_texts[-1] == "Which items were damaged?"


async def test_a_temporary_llm_failure_retries_the_whole_turn_once(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.llm.script(LLMTemporaryError("timeout"), ask(merchant_name="DoorDash"))
    await h.say(COMPLAINT)
    # The failed attempt left nothing behind.
    assert await h.cases() == []
    assert h.telegram.sent_texts == []

    h.clock.advance(timedelta(minutes=1))
    await h.worker.run_until_idle()

    case = await h.only_case()
    assert [f.key for f in await h.facts(case)] == ["merchant_name"]
    assert h.telegram.sent_texts == [QUESTION]


async def test_invalid_model_output_gives_up_and_tells_the_user(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.llm.script({"facts": "not a list"})
    await h.say(COMPLAINT)
    await h.worker.run_until_idle()  # delivers the notice queued after the failure

    assert await h.cases() == []
    [notice] = h.telegram.sent_texts
    assert "went wrong" in notice
    statuses = (await session.scalars(select(Event.status).order_by(Event.id))).all()
    assert statuses[0] is EventStatus.DEAD


async def test_a_message_goes_to_the_focused_intake_case(
    database: Database, session: AsyncSession, user: User
) -> None:
    h = Harness(database, session)
    h.llm.script(ask(merchant_name="DoorDash"), ask("And the order?", issue_type="missing_item"))
    await h.say(COMPLAINT)
    await h.say("they forgot the fries")

    case = await h.only_case()
    assert set(await h.current(case)) == {"merchant_name", "issue_type"}

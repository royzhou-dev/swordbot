"""The draft service and the approval check behind the send guard (PLAN D2)."""

import uuid
from datetime import UTC, datetime
from typing import Literal, cast

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions import service as action_service
from app.actions.models import ActionKind, PendingAction
from app.actions.service import ButtonSpec
from app.cases import service as case_service
from app.cases.models import SupportCase, TransitionActor
from app.email import drafts
from app.email.approvals import DraftButtonPayload, EmailApprovalVerifier
from app.email.drafts import SIGN_OFF, compose_body, content_hash
from app.email.errors import DraftLockedError
from app.email.models import OutboundEmail, OutboundEmailStatus
from app.events.models import EventSource, EventType
from app.events.schemas import ClaimedEvent
from app.logging import get_logger
from app.telegram.delivery import TelegramOutbox
from app.tools.email_tools import DraftSupportEmail
from app.tools.errors import ToolApprovalRequiredError
from app.tools.registry import AnyTool, Tool, ToolContext, ToolExecutor, ToolRegistry, ToolRiskLevel
from app.users.models import User

S = OutboundEmailStatus
NOW = datetime(2026, 9, 27, tzinfo=UTC)
TO = "support@example.com"


async def _case(session: AsyncSession, user: User) -> SupportCase:
    case = await case_service.create_case(session, user_id=user.id, actor=TransitionActor.USER)
    await session.commit()
    return case


async def _version(
    session: AsyncSession, case: SupportCase, body: str = "Hi,\n\nPlease refund me.", **kw: str
) -> OutboundEmail:
    new = await drafts.create_version(
        session,
        case,
        to_address=kw.get("to", TO),
        subject=kw.get("subject", "Refund"),
        body_text=body,
        signature_name="Test User",
        event_id=None,
    )
    await session.commit()
    return new.email


async def _send_button(session: AsyncSession, email: OutboundEmail, event_id: int) -> uuid.UUID:
    """Show Send/Edit buttons for `email` and press Send. Returns the Send action id."""
    payload = DraftButtonPayload(
        outbound_email_id=email.id, content_hash=email.content_hash
    ).model_dump(mode="json")
    group = await action_service.create_group(
        session,
        user_id=email.user_id,
        buttons=[
            ButtonSpec(ActionKind.SEND_EMAIL, "Send", payload),
            ButtonSpec(ActionKind.EDIT_DRAFT, "Edit", payload),
        ],
    )
    [send, _] = await action_service.open_actions_in_group(
        session, group, user_id=email.user_id, now=NOW
    )
    result = await action_service.consume(
        session, send.id, user_id=email.user_id, event_id=event_id, now=NOW
    )
    assert result.accepted
    await session.commit()
    return send.id


async def _approve(
    session: AsyncSession, email: OutboundEmail, event_id: int, action_id: uuid.UUID
) -> OutboundEmail | None:
    approved = await drafts.approve(
        session,
        email.id,
        user_id=email.user_id,
        expected_hash=email.content_hash,
        event_id=event_id,
        action_id=action_id,
        now=NOW,
    )
    await session.commit()
    return approved


# --- Content and hashing -----------------------------------------------------------


def test_the_hash_covers_recipient_subject_and_body() -> None:
    base = content_hash(TO, "Refund", "body")
    assert base == content_hash(TO, "Refund", "body")
    assert base != content_hash("help@example.com", "Refund", "body")
    assert base != content_hash(TO, "Refund!", "body")
    assert base != content_hash(TO, "Refund", "body ")
    # Fields can't bleed into each other.
    assert content_hash("a", "bc", "d") != content_hash("ab", "c", "d")


def test_code_adds_the_sign_off() -> None:
    assert compose_body("Hi,\n\nRefund please.\n", "Alex Kim") == (
        f"Hi,\n\nRefund please.\n\n{SIGN_OFF}\nAlex Kim"
    )
    assert compose_body("Hi.", None) == f"Hi.\n\n{SIGN_OFF}"


# --- Versions -----------------------------------------------------------------------


async def test_a_new_version_supersedes_the_one_awaiting_approval(
    session: AsyncSession, user: User
) -> None:
    case = await _case(session, user)
    v1 = await _version(session, case)
    v2 = await _version(session, case, "Hi,\n\nShorter.")

    await session.refresh(v1)
    assert (v1.version, v1.status) == (1, S.SUPERSEDED)
    assert (v2.version, v2.status, v2.supersedes_id) == (2, S.AWAITING_APPROVAL, v1.id)
    assert await drafts.live_email(session, case) == v2


async def test_an_approved_email_cant_be_replaced(
    session: AsyncSession, user: User, event_id: int
) -> None:
    case = await _case(session, user)
    email = await _version(session, case)
    await _approve(session, email, event_id, await _send_button(session, email, event_id))

    with pytest.raises(DraftLockedError):
        await _version(session, case, "Hi,\n\nSomething else.")


async def test_the_database_allows_one_live_email_per_case(
    session: AsyncSession, user: User
) -> None:
    case = await _case(session, user)
    email = await _version(session, case)
    twin = OutboundEmail(
        user_id=user.id,
        case_id=case.id,
        version=2,
        status=S.APPROVED,
        to_address=email.to_address,
        subject=email.subject,
        body=email.body,
        body_text=email.body_text,
        content_hash=email.content_hash,
    )
    session.add(twin)
    with pytest.raises(IntegrityError):
        await session.flush()


# --- Approval -----------------------------------------------------------------------


async def test_approval_is_bound_to_the_exact_content(
    session: AsyncSession, user: User, event_id: int
) -> None:
    case = await _case(session, user)
    email = await _version(session, case)
    action_id = await _send_button(session, email, event_id)

    stale = await drafts.approve(
        session,
        email.id,
        user_id=user.id,
        expected_hash=content_hash(TO, "Refund", "something else"),
        event_id=event_id,
        action_id=action_id,
        now=NOW,
    )
    assert stale is None
    approved = await _approve(session, email, event_id, action_id)
    assert approved is not None
    assert (approved.status, approved.approved_by_action_id) == (S.APPROVED, action_id)
    assert approved.approved_at == NOW


async def test_approving_twice_succeeds_once(
    session: AsyncSession, user: User, event_id: int
) -> None:
    case = await _case(session, user)
    email = await _version(session, case)
    action_id = await _send_button(session, email, event_id)

    assert await _approve(session, email, event_id, action_id) is not None
    assert await _approve(session, email, event_id, action_id) is None


async def test_a_superseded_version_cant_be_approved(
    session: AsyncSession, user: User, event_id: int
) -> None:
    case = await _case(session, user)
    v1 = await _version(session, case)
    action_id = await _send_button(session, v1, event_id)
    await _version(session, case, "Hi,\n\nNewer.")

    assert await _approve(session, v1, event_id, action_id) is None


async def test_another_users_email_cant_be_approved(
    session: AsyncSession, user: User, event_id: int
) -> None:
    case = await _case(session, user)
    email = await _version(session, case)
    action_id = await _send_button(session, email, event_id)

    other = await drafts.approve(
        session,
        email.id,
        user_id=uuid.uuid4(),
        expected_hash=email.content_hash,
        event_id=event_id,
        action_id=action_id,
        now=NOW,
    )
    assert other is None


async def test_discarding_cancels_an_unsent_email(
    session: AsyncSession, user: User, event_id: int
) -> None:
    case = await _case(session, user)
    email = await _version(session, case)
    await _approve(session, email, event_id, await _send_button(session, email, event_id))

    discarded = await drafts.discard_live(session, case, to_status=S.CANCELLED)
    assert discarded is not None and discarded.status is S.CANCELLED
    assert await drafts.live_email(session, case) is None
    assert await drafts.discard_live(session, case, to_status=S.CANCELLED) is None


# --- The approval verifier (for M7's send tool) -------------------------------------


class SendEmail(BaseModel):
    tool: Literal["send_support_email"]
    outbound_email_id: uuid.UUID


class Sent(BaseModel):
    pass


def _send_tool(runs: list[SendEmail]) -> AnyTool:
    async def run(ctx: ToolContext, args: SendEmail) -> Sent:
        runs.append(args)
        return Sent()

    return Tool(
        name="send_support_email",
        description="test",
        risk=ToolRiskLevel.REQUIRES_APPROVAL,
        args_model=SendEmail,
        result_model=Sent,
        run=run,
    )


def _ctx(session: AsyncSession, user: User, event_id: int) -> ToolContext:
    event = ClaimedEvent(
        id=event_id,
        user_id=user.id,
        case_id=None,
        type=EventType.USER_BUTTON_ACTION,
        source=EventSource.DEV,
        external_id="x",
        payload={},
        attempts=1,
        claim_token=uuid.uuid4(),
        created_at=NOW,
    )
    return ToolContext(
        session=session,
        event=event,
        now=NOW,
        log=get_logger("test"),
        outbox=cast(TelegramOutbox, None),
    )


async def _send(
    session: AsyncSession,
    user: User,
    event_id: int,
    email: OutboundEmail,
    approval_id: uuid.UUID | None,
) -> list[SendEmail]:
    runs: list[SendEmail] = []
    executor = ToolExecutor(ToolRegistry([_send_tool(runs)]), EmailApprovalVerifier())
    call = SendEmail(tool="send_support_email", outbound_email_id=email.id)
    await executor.execute(
        call,
        _ctx(session, user, event_id),
        allowed=frozenset({"send_support_email"}),
        approval_id=approval_id,
    )
    return runs


async def test_the_send_press_that_approved_the_email_authorizes_it(
    session: AsyncSession, user: User, event_id: int
) -> None:
    case = await _case(session, user)
    email = await _version(session, case)
    action_id = await _send_button(session, email, event_id)
    await _approve(session, email, event_id, action_id)

    runs = await _send(session, user, event_id, email, action_id)
    assert [r.outbound_email_id for r in runs] == [email.id]


async def test_a_consumed_send_press_is_not_enough_without_the_approval(
    session: AsyncSession, user: User, event_id: int
) -> None:
    case = await _case(session, user)
    email = await _version(session, case)
    action_id = await _send_button(session, email, event_id)

    with pytest.raises(ToolApprovalRequiredError):
        await _send(session, user, event_id, email, action_id)


async def test_other_buttons_and_other_emails_are_not_approvals(
    session: AsyncSession, user: User, event_id: int
) -> None:
    case = await _case(session, user)
    email = await _version(session, case)
    action_id = await _send_button(session, email, event_id)
    await _approve(session, email, event_id, action_id)
    edit = (
        await session.scalars(
            select(PendingAction).where(PendingAction.kind == ActionKind.EDIT_DRAFT)
        )
    ).one()

    with pytest.raises(ToolApprovalRequiredError):
        await _send(session, user, event_id, email, edit.id)
    with pytest.raises(ToolApprovalRequiredError):
        await _send(session, user, event_id, email, uuid.uuid4())

    other_case = await _case(session, user)
    other = await _version(session, other_case)
    with pytest.raises(ToolApprovalRequiredError):
        await _send(session, user, event_id, other, action_id)


async def test_content_changed_after_approval_is_not_approved(
    session: AsyncSession, user: User, event_id: int
) -> None:
    case = await _case(session, user)
    email = await _version(session, case)
    action_id = await _send_button(session, email, event_id)
    await _approve(session, email, event_id, action_id)

    # Nothing in the app edits an email in place; this simulates a bug or tampering.
    email.body += "\nP.S. Also send me a gift card."
    await session.commit()

    with pytest.raises(ToolApprovalRequiredError):
        await _send(session, user, event_id, email, action_id)


# --- What the model may write --------------------------------------------------------


def _draft(subject: str = "Refund", body: str = "Hi,\n\nPlease refund me.") -> DraftSupportEmail:
    return DraftSupportEmail.model_validate(
        {"tool": "draft_support_email", "subject": subject, "body": body}
    )


@pytest.mark.parametrize(
    "body",
    [
        "Hi,\n\nMy order [Order Number] was late.",
        "Hi,\n\nPlease refund me.\n\n[Your Name]",
        "Hi {{name}},\n\nRefund please.",
        "Hi,\n\nOrder <ORDER NUMBER> was late.",
    ],
)
def test_placeholders_are_rejected(body: str) -> None:
    with pytest.raises(ValueError, match="placeholders"):
        _draft(body=body)


@pytest.mark.parametrize(
    "closing",
    [
        "Thanks,",
        "Best regards,",
        "Thank you!",
        "Sincerely",
        "Many thanks,",
        "Thanks in advance!",
        "Thank you so much,",
        "With kind regards,",
        "Best wishes,",
        "Yours truly,",
        "All the best,",
    ],
)
def test_a_sign_off_is_rejected(closing: str) -> None:
    with pytest.raises(ValueError, match="sign-off"):
        _draft(body=f"Hi,\n\nPlease refund me.\n\n{closing}")


@pytest.mark.parametrize(
    "ending",
    [
        "Best regards,\nJane Smith",
        "Thanks,\nRoy\nCustomer",
        "Many thanks!\nA. Kim\n555-0100",
    ],
)
def test_a_sign_off_followed_by_a_name_is_rejected(ending: str) -> None:
    # Code signs the email; a model-written name would be a second, possibly invented, one.
    with pytest.raises(ValueError, match="sign-off"):
        _draft(body=f"Hi,\n\nPlease refund me.\n\n{ending}")


@pytest.mark.parametrize(
    "body",
    [
        "Hi,\n\nPlease refund me. Thank you for your help.",
        "Hi,\n\nPlease refund me. Thanks!",
        "Hi,\n\nThank you for your help.",
    ],
)
def test_a_closing_sentence_is_fine(body: str) -> None:
    assert _draft(body=body).body == body


def test_the_subject_is_one_bounded_line() -> None:
    with pytest.raises(ValueError, match="single line"):
        _draft(subject="Refund\nplease")
    with pytest.raises(ValueError, match="at most"):
        _draft(subject="x" * 151)
    assert _draft(subject="  Refund  ").subject == "Refund"

"""The `SEND_EMAIL` handler, and settling a send that didn't finish (M7, PLAN D14).

The Send press approves the email and queues a `SEND_EMAIL` event. The send
can't run inside the button handler: that handler has already written the
approval, and the claim must commit in its own transaction before Gmail is
called, which it couldn't do past the handler's own lock.

What the handler does depends on the email's status when it runs:

- `approved`: send it through `send_support_email` (see `app.tools.send_tools`);
- `sending`: an earlier attempt claimed it and never recorded the outcome
  (it crashed or timed out, perhaps after Gmail accepted it). The outcome is
  unknown, so it is never sent again: the user is asked to check Gmail;
- anything else: a duplicate event, or the email was cancelled; nothing to do.
"""

from app.cases import service as case_service
from app.cases.models import CaseStatus, SupportCase, TransitionActor
from app.cases.schemas import CaseFieldsUpdate
from app.cases.state_machine import transition
from app.db.session import Database
from app.email import drafts
from app.email.approvals import EmailApprovalVerifier
from app.email.gmail_client import GmailClient
from app.email.models import OutboundEmail, OutboundEmailStatus
from app.events.errors import PermanentEventError
from app.events.handlers import HandlerContext
from app.events.schemas import SendEmailPayload
from app.telegram.delivery import TelegramOutbox
from app.tools.chat_tools import say
from app.tools.registry import ToolContext, ToolExecutor, ToolRegistry
from app.tools.send_tools import (
    GAVE_UP_REPLY,
    SendResult,
    SendSupportEmail,
    ask_if_sent,
    has_open_buttons,
    offer_again,
    send_support_email_tool,
)

S = OutboundEmailStatus

SEND_TOOLS = frozenset({"send_support_email"})

SENT_REPLY = "Sent to {to}.\nSubject: {subject}\n\nI'll keep the case open for their reply."
CONFIRMED_SENT_REPLY = "Thanks. I'll treat it as sent and keep the case open for their reply."
NOT_SENT_REPLY = "OK, so nothing went out."
ANSWER_ABOVE_REPLY = "Did your email go out? Please use the buttons above to tell me."
STALE_REPLY = "That question has already been answered."


class EmailSender:
    """The `SEND_EMAIL` handler."""

    def __init__(
        self, database: Database, gmail: GmailClient, *, sender_address: str | None
    ) -> None:
        tool = send_support_email_tool(database, gmail, sender_address=sender_address)
        self._executor = ToolExecutor(ToolRegistry([tool]), approvals=EmailApprovalVerifier())

    async def __call__(self, ctx: HandlerContext, payload: SendEmailPayload) -> None:
        # Nothing in here writes to ctx.session before the claim (PLAN D14).
        session = ctx.session
        email = await drafts.get_email(
            session, payload.outbound_email_id, user_id=ctx.event.user_id
        )
        case = await case_service.get_case(session, email.case_id, user_id=ctx.event.user_id)
        tools = await _tool_context(ctx, case)
        if email.status is S.SENDING:
            # An earlier attempt claimed it and didn't record what happened.
            tools.log.warning("email_send_outcome_unknown", outbound_email_id=str(email.id))
            await drafts.mark_needs_attention(session, email, now=ctx.now)
            await ask_if_sent(tools, email)
            return
        if email.status is not S.APPROVED or case.status is not CaseStatus.READY_TO_SEND:
            tools.log.info(
                "email_send_skipped", email_status=email.status.value, status=case.status.value
            )
            return
        result = await self._executor.execute(
            SendSupportEmail(tool="send_support_email", outbound_email_id=email.id),
            tools,
            allowed=SEND_TOOLS,
            approval_id=email.approved_by_action_id,
        )
        if not isinstance(result, SendResult):
            raise TypeError(f"send_support_email returned {type(result).__name__}")
        await _conclude(tools, case, email, result)


async def _conclude(
    ctx: ToolContext, case: SupportCase, email: OutboundEmail, result: SendResult
) -> None:
    match result.outcome:
        case "sent":
            await case_service.update_fields(
                ctx.session, case, CaseFieldsUpdate(gmail_thread_id=email.gmail_thread_id)
            )
            await _move(ctx, case, CaseStatus.WAITING_FOR_SUPPORT, f"email v{email.version} sent")
            # Invariant 8: what was sent, and to whom.
            await say(ctx, SENT_REPLY.format(to=email.to_address, subject=email.subject))
        case "failed":
            await _move(ctx, case, CaseStatus.WAITING_FOR_USER_APPROVAL, "send failed; not sent")
            await offer_again(ctx, case, email, notice=result.notice or GAVE_UP_REPLY)
        case "needs_attention":
            await ask_if_sent(ctx, email)
        case "skipped":
            ctx.log.info("email_send_skipped", outbound_email_id=str(email.id))


async def resume_unfinished_send(ctx: ToolContext, case: SupportCase) -> None:
    """The user wrote while the case is READY_TO_SEND: settle whatever the send left behind.

    A user's events run in order (PLAN D1), so the case's `SEND_EMAIL` event,
    queued before this message, has already finished or given up.
    """
    email = await drafts.latest_version(ctx.session, case)
    if email is None:
        raise PermanentEventError(f"case {case.id} is ready to send without an email")
    match email.status:
        case S.APPROVED:
            # The send gave up before claiming (e.g. Gmail was unreachable): nothing went out.
            await drafts.mark_failed(ctx.session, email, from_status=S.APPROVED, now=ctx.now)
            await _move(ctx, case, CaseStatus.WAITING_FOR_USER_APPROVAL, "send gave up")
            await offer_again(ctx, case, email, notice=GAVE_UP_REPLY)
        case S.SENDING:
            await drafts.mark_needs_attention(ctx.session, email, now=ctx.now)
            await ask_if_sent(ctx, email)
        case S.NEEDS_ATTENTION if await has_open_buttons(ctx, email):
            await say(ctx, ANSWER_ABOVE_REPLY)
        case S.NEEDS_ATTENTION:
            await ask_if_sent(ctx, email)
        case _:
            ctx.log.warning("ready_to_send_without_a_pending_send", email_status=email.status.value)


async def confirm_sent(ctx: ToolContext, case: SupportCase, email: OutboundEmail) -> None:
    """[It was sent]: the user found the email in Gmail's Sent folder."""
    if not await drafts.confirm_sent(ctx.session, email, now=ctx.now):
        await say(ctx, STALE_REPLY)
        return
    await _move(
        ctx,
        case,
        CaseStatus.WAITING_FOR_SUPPORT,
        "user confirmed the email was sent",
        actor=TransitionActor.USER,
    )
    await say(ctx, CONFIRMED_SENT_REPLY)


async def confirm_not_sent(ctx: ToolContext, case: SupportCase, email: OutboundEmail) -> None:
    """[It wasn't sent]: offer the same email again for approval."""
    moved = await drafts.mark_failed(ctx.session, email, from_status=S.NEEDS_ATTENTION, now=ctx.now)
    if not moved:
        await say(ctx, STALE_REPLY)
        return
    await _move(
        ctx,
        case,
        CaseStatus.WAITING_FOR_USER_APPROVAL,
        "user says it wasn't sent",
        actor=TransitionActor.USER,
    )
    await offer_again(ctx, case, email, notice=NOT_SENT_REPLY)


async def _tool_context(ctx: HandlerContext, case: SupportCase) -> ToolContext:
    return ToolContext(
        session=ctx.session,
        event=ctx.event,
        now=ctx.now,
        log=ctx.log.bind(case_id=str(case.id)),
        outbox=await TelegramOutbox.for_event(ctx),
        case=case,
    )


async def _move(
    ctx: ToolContext,
    case: SupportCase,
    to_status: CaseStatus,
    reason: str,
    *,
    actor: TransitionActor = TransitionActor.SYSTEM,
) -> None:
    await transition(
        ctx.session,
        case,
        to_status,
        reason=reason,
        actor=actor,
        event_id=ctx.event.id,
    )

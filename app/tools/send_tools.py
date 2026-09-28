"""Sending an approved email, and what the user sees when a send doesn't go through (M7).

`send_support_email` is `REQUIRES_APPROVAL`: the tool executor runs it only
with the consumed Send press that approved this exact email
(`EmailApprovalVerifier`, PLAN D2). The agent loop never passes an approval,
so the model can't send anything.

The send follows PLAN D14. Everything that may fail harmlessly (getting an
access token, building the message) happens first, with the email still
approved. Then `approved -> sending` is committed in its own transaction, and
only then is Gmail called. After that, nothing that goes wrong can lead to a
second send: an error that proves Gmail didn't get the email marks it
`failed`, and anything else marks it `needs_attention` for the user to check.
A crash leaves it `sending`, which the retry treats the same way.
"""

import uuid
from typing import Literal

from pydantic import BaseModel

from app.actions import service as action_service
from app.actions.models import ActionKind
from app.actions.service import ButtonSpec
from app.cases import messages as case_messages
from app.cases.models import CaseStatus, MessageRole, SupportCase
from app.db.session import Database
from app.email import drafts
from app.email.errors import GmailAuthenticationError, GmailPermanentError, provably_not_sent
from app.email.gmail_client import GmailClient
from app.email.mime import compose_mime
from app.email.models import OutboundEmail, OutboundEmailStatus
from app.tools.chat_tools import say
from app.tools.email_tools import present_draft
from app.tools.registry import Tool, ToolContext, ToolRiskLevel

S = OutboundEmailStatus

NOT_CONNECTED_REPLY = (
    "I couldn't send the email: Gmail isn't connected, or its access was revoked. Nothing went out."
)
REJECTED_REPLY = "Gmail refused to send the email, so nothing went out."
UNREACHABLE_REPLY = "I couldn't reach Gmail, so nothing went out."
GAVE_UP_REPLY = "I couldn't send your email earlier, and nothing went out."
OFFER_AGAIN_INTRO = "Here it is again. Tap Send to try once more."
ASK_IF_SENT_PROMPT = (
    "I couldn't confirm that your email to {to} went out. Please check the Sent folder "
    'in Gmail for "{subject}" and tell me what you find.'
)


class SendSupportEmail(BaseModel):
    """Send an approved email version to the merchant's support."""

    tool: Literal["send_support_email"]
    outbound_email_id: uuid.UUID


class SendResult(BaseModel):
    # "skipped": the email wasn't approved any more when the claim ran.
    outcome: Literal["sent", "failed", "needs_attention", "skipped"]
    # For "failed": what to tell the user.
    notice: str | None = None


def send_support_email_tool(
    database: Database, gmail: GmailClient, *, sender_address: str | None
) -> Tool[SendSupportEmail, SendResult]:
    async def run(ctx: ToolContext, args: SendSupportEmail) -> SendResult:
        email = await drafts.get_email(
            ctx.session, args.outbound_email_id, user_id=ctx.event.user_id
        )
        # 1. Before the claim, with the email still approved. Temporary errors
        #    propagate and the worker retries; nothing has been sent.
        try:
            await gmail.authorize()
        except GmailPermanentError as exc:
            await drafts.mark_failed(ctx.session, email, from_status=S.APPROVED, now=ctx.now)
            return SendResult(outcome="failed", notice=_failure_notice(exc))
        raw = compose_mime(email, sender_address=sender_address)

        # 2. The claim, committed on its own before Gmail is called. The handler
        #    has written nothing in its own transaction yet, so this can't block
        #    on a lock it holds itself (PLAN D14).
        async with database.transaction() as claim_session:
            claimed = await drafts.claim_for_sending(claim_session, email.id, now=ctx.now)
        if not claimed:
            return SendResult(outcome="skipped")
        await ctx.session.refresh(email)

        # 3. The call. From here on the email is never sent again automatically.
        try:
            sent = await gmail.send(raw)
        except Exception as exc:
            ctx.log.warning("email_send_error", error_type=type(exc).__name__)
            if provably_not_sent(exc):
                await drafts.mark_failed(ctx.session, email, from_status=S.SENDING, now=ctx.now)
                return SendResult(outcome="failed", notice=_failure_notice(exc))
            await drafts.mark_needs_attention(ctx.session, email, now=ctx.now)
            return SendResult(outcome="needs_attention")
        await drafts.mark_sent(
            ctx.session,
            email,
            gmail_message_id=sent.message_id,
            gmail_thread_id=sent.thread_id,
            now=ctx.now,
        )
        return SendResult(outcome="sent")

    return Tool(
        name="send_support_email",
        description="Send an approved email to the merchant's support.",
        risk=ToolRiskLevel.REQUIRES_APPROVAL,
        args_model=SendSupportEmail,
        result_model=SendResult,
        run=run,
        terminal=True,
    )


def _failure_notice(exc: BaseException) -> str:
    if isinstance(exc, GmailAuthenticationError):
        return NOT_CONNECTED_REPLY
    if isinstance(exc, GmailPermanentError):
        return REJECTED_REPLY
    return UNREACHABLE_REPLY


async def offer_again(
    ctx: ToolContext, case: SupportCase, failed: OutboundEmail, *, notice: str
) -> None:
    """After a send that certainly didn't go out: the same email as a new version to approve.

    The case must already be back in WAITING_FOR_USER_APPROVAL.
    """
    new = await drafts.create_version(
        ctx.session,
        case,
        to_address=failed.to_address,
        subject=failed.subject,
        body_text=failed.body_text,
        signature_name=failed.signature_name,
        event_id=ctx.event.id,
    )
    await say(ctx, notice)
    await present_draft(ctx, new.email, intro=OFFER_AGAIN_INTRO)


async def ask_if_sent(ctx: ToolContext, email: OutboundEmail) -> None:
    """After a send whose outcome is unknown: [It was sent] [It wasn't sent].

    The case stays READY_TO_SEND until the user answers.
    """
    payload = {"outbound_email_id": str(email.id)}
    group_id = await action_service.create_group(
        ctx.session,
        user_id=email.user_id,
        buttons=[
            ButtonSpec(ActionKind.CONFIRM_SENT, "It was sent", payload),
            ButtonSpec(ActionKind.CONFIRM_NOT_SENT, "It wasn't sent", payload),
        ],
        case_id=email.case_id,
        expected_case_status=CaseStatus.READY_TO_SEND,
    )
    await drafts.attach_buttons(ctx.session, email, group_id)
    text = ASK_IF_SENT_PROMPT.format(to=email.to_address, subject=email.subject)
    await ctx.outbox.send_message(text, action_group_id=group_id)
    if ctx.case is not None:
        await case_messages.add_message(
            ctx.session, ctx.case, MessageRole.ASSISTANT, text, event_id=ctx.event.id
        )


async def has_open_buttons(ctx: ToolContext, email: OutboundEmail) -> bool:
    return email.action_group_id is not None and await action_service.has_open_actions(
        ctx.session, email.action_group_id, user_id=email.user_id, now=ctx.now
    )

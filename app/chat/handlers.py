"""Handlers for what the user does in chat: messages and button presses.

A text message is either a command (`/start`, `/help`, `/cancel`), handled
here in code, or goes where `cases.routing` sends it: the intake conversation
(`app.agent.intake`) or the review of a draft waiting for approval
(`app.agent.drafting`). Buttons run through `pending_actions` (PLAN D3);
`_run_action` has one branch per `ActionKind`.

Approval is only ever a Send press (Invariant 2): `SEND_EMAIL` is the only
code that calls `drafts.approve`.
"""

import uuid
from datetime import timedelta
from typing import assert_never

from pydantic import ValidationError

from app.actions import service as action_service
from app.actions.models import ActionKind, PendingAction
from app.actions.service import ButtonSpec
from app.agent.drafting import DraftingAgent
from app.agent.intake import IntakeAgent
from app.cases import service as case_service
from app.cases.models import CaseStatus, SupportCase, TransitionActor
from app.cases.routing import Stage, route_message
from app.cases.state_machine import CLOSED_STATUSES, transition
from app.email import drafts, sending
from app.email.approvals import DraftButtonPayload
from app.email.models import OutboundEmailStatus
from app.events import service as event_service
from app.events.errors import PermanentEventError
from app.events.handlers import HandlerContext
from app.events.models import EventSource, EventType
from app.events.schemas import (
    ButtonPressPayload,
    NewEvent,
    SendEmailPayload,
    UserMessagePayload,
)
from app.telegram.delivery import TelegramOutbox
from app.tools.chat_tools import say
from app.tools.email_tools import ensure_draft_buttons, retire_buttons
from app.tools.registry import ToolContext

CONFIRMATION_LIFETIME = timedelta(hours=24)

NOT_TEXT_REPLY = "I can only read text messages for now."
STALE_BUTTON_REPLY = "This button is no longer valid."
HELP_TEXT = (
    "I help you sort out problems with online orders: missing or wrong items, damage, "
    "late deliveries, billing mistakes.\n\n"
    'Just tell me what happened, for example "My DoorDash order was missing the fries." '
    "I'll ask for anything else I need, then draft an email to the merchant's support for "
    "you to approve. Nothing is sent without your OK.\n\n"
    "/cancel - drop the case you're working on\n"
    "/help - show this message"
)
UNKNOWN_COMMAND_REPLY = "I don't know that command.\n\n" + HELP_TEXT
NO_CASE_TO_CANCEL_REPLY = "You have no open case to cancel."
CANCELLED_REPLY = "Cancelled. I won't contact {merchant} about it."
KEPT_REPLY = "OK, I'll keep working on it."
EDIT_PROMPT = (
    'What would you like to change? For example "make it shorter" or '
    '"ask for a replacement instead".'
)
APPROVED_REPLY = "Approved. Sending it to {to} now."
CANCELLED_MAYBE_SENT_REPLY = (
    "Cancelled. Your email to {to} may already have gone out, so check Gmail's Sent folder. "
    "I won't contact them again about it."
)
STALE_DRAFT_REPLY = "That draft has changed since. Use the buttons under the latest version."


class UserMessageHandler:
    """The `USER_MESSAGE` handler."""

    def __init__(self, intake: IntakeAgent, drafting: DraftingAgent) -> None:
        self._intake = intake
        self._drafting = drafting

    async def __call__(self, ctx: HandlerContext, payload: UserMessagePayload) -> None:
        outbox = await TelegramOutbox.for_event(ctx)
        text = (payload.text or "").strip()
        if not text:
            await outbox.send_message(NOT_TEXT_REPLY)
            return
        if text.startswith("/"):
            await _run_command(ctx, outbox, text)
            return
        route = await route_message(ctx.session, ctx.event.user_id)
        match route.stage:
            case Stage.INTAKE:
                await self._intake.handle(
                    ctx,
                    outbox,
                    text,
                    case=route.case,
                    sent_case=route.sent,
                    telegram_message_id=payload.telegram_message_id,
                )
            case Stage.DRAFT_REVIEW:
                if route.case is None:
                    raise PermanentEventError("draft review was routed without a case")
                await self._drafting.review(
                    ctx, outbox, route.case, text, telegram_message_id=payload.telegram_message_id
                )
            case Stage.APPROVED:
                if route.case is None:
                    raise PermanentEventError("an approved stage was routed without a case")
                await sending.resume_unfinished_send(
                    _tool_context(ctx, outbox, route.case), route.case
                )
            case _:
                assert_never(route.stage)


def _command_name(text: str) -> str:
    """`/Cancel@my_bot now` -> `cancel`."""
    return text.split(maxsplit=1)[0].removeprefix("/").split("@", 1)[0].lower()


async def _run_command(ctx: HandlerContext, outbox: TelegramOutbox, text: str) -> None:
    match _command_name(text):
        case "start" | "help":
            await outbox.send_message(HELP_TEXT)
        case "cancel":
            case = await case_service.get_focused_case(ctx.session, ctx.event.user_id)
            await _offer_cancel(ctx, outbox, case)
        case _:
            await outbox.send_message(UNKNOWN_COMMAND_REPLY)


async def _offer_cancel(
    ctx: HandlerContext, outbox: TelegramOutbox, case: SupportCase | None
) -> None:
    """Ask for confirmation with buttons. Only the button cancels."""
    if case is None or case.status in CLOSED_STATUSES:
        await outbox.send_message(NO_CASE_TO_CANCEL_REPLY)
        return
    group_id = await action_service.create_group(
        ctx.session,
        user_id=ctx.event.user_id,
        buttons=[
            ButtonSpec(ActionKind.CANCEL_CASE, "Yes, cancel"),
            ButtonSpec(ActionKind.KEEP_CASE, "Keep it"),
        ],
        case_id=case.id,
        # A confirmation shown before the case moved on can't cancel it.
        expected_case_status=case.status,
        expires_at=ctx.now + CONFIRMATION_LIFETIME,
    )
    subject = f"your {case.merchant_name} case" if case.merchant_name else "this case"
    await outbox.send_message(
        f"Cancel {subject}? I'll stop working on it and won't contact support about it.",
        action_group_id=group_id,
    )


async def handle_button_press(ctx: HandlerContext, payload: ButtonPressPayload) -> None:
    outbox = await TelegramOutbox.for_event(ctx)
    result = await action_service.consume(
        ctx.session,
        payload.action_id,
        user_id=ctx.event.user_id,
        event_id=ctx.event.id,
        now=ctx.now,
    )
    # Pressed or not, the buttons on that message are done with.
    if payload.telegram_message_id is not None:
        await outbox.clear_buttons(payload.telegram_message_id)
    if not result.accepted or result.action is None:
        await outbox.answer_callback(payload.callback_query_id, STALE_BUTTON_REPLY)
        return
    await outbox.answer_callback(payload.callback_query_id)
    await _run_action(ctx, outbox, result.action)


def _tool_context(
    ctx: HandlerContext, outbox: TelegramOutbox, case: SupportCase | None
) -> ToolContext:
    return ToolContext(
        session=ctx.session,
        event=ctx.event,
        now=ctx.now,
        log=ctx.log.bind(case_id=str(case.id)) if case else ctx.log,
        outbox=outbox,
        case=case,
    )


async def _run_action(ctx: HandlerContext, outbox: TelegramOutbox, action: PendingAction) -> None:
    case = await _action_case(ctx, action)
    tools = _tool_context(ctx, outbox, case)
    match action.kind:
        case ActionKind.CANCEL_CASE:
            await _cancel_case(ctx, tools, _require(case, action))
        case ActionKind.CONFIRM_SENT | ActionKind.CONFIRM_NOT_SENT:
            case = _require(case, action)
            email = await drafts.get_email(
                ctx.session, _payload_email_id(action), user_id=ctx.event.user_id
            )
            if action.kind is ActionKind.CONFIRM_SENT:
                await sending.confirm_sent(tools, case, email)
            else:
                await sending.confirm_not_sent(tools, case, email)
        case ActionKind.KEEP_CASE:
            await outbox.send_message(KEPT_REPLY)
            if case is not None and case.status is CaseStatus.WAITING_FOR_USER_APPROVAL:
                # Pressing Cancel under the draft closed its Send button.
                await ensure_draft_buttons(tools, case)
        case ActionKind.SEND_EMAIL:
            await _approve(ctx, tools, _require(case, action), action)
        case ActionKind.EDIT_DRAFT:
            await say(tools, EDIT_PROMPT)
        case ActionKind.CANCEL_DRAFT:
            await _offer_cancel(ctx, outbox, _require(case, action))
        case _:
            # mypy flags any ActionKind without a branch.
            assert_never(action.kind)


async def _approve(
    ctx: HandlerContext, tools: ToolContext, case: SupportCase, action: PendingAction
) -> None:
    """The Send press: approve exactly the content that was on screen."""
    try:
        button = DraftButtonPayload.model_validate(action.payload)
    except ValidationError:
        raise PermanentEventError(f"action {action.id} has an invalid draft payload") from None
    email = await drafts.approve(
        ctx.session,
        button.outbound_email_id,
        user_id=ctx.event.user_id,
        expected_hash=button.content_hash,
        event_id=ctx.event.id,
        action_id=action.id,
        now=ctx.now,
    )
    if email is None:
        await say(tools, STALE_DRAFT_REPLY)
        return
    await transition(
        ctx.session,
        case,
        CaseStatus.READY_TO_SEND,
        reason=f"user approved email version {email.version}",
        actor=TransitionActor.USER,
        event_id=ctx.event.id,
    )
    # The send is its own event (PLAN D14): its claim has to commit before
    # Gmail is called, which this handler can't do after writing the approval.
    await event_service.enqueue(
        ctx.session,
        NewEvent(
            user_id=case.user_id,
            type=EventType.SEND_EMAIL,
            source=EventSource.SYSTEM,
            external_id=f"send:{email.id}",
            payload=SendEmailPayload(outbound_email_id=email.id).model_dump(mode="json"),
            case_id=case.id,
        ),
        now=ctx.now,
    )
    await say(tools, APPROVED_REPLY.format(to=email.to_address))


async def _cancel_case(ctx: HandlerContext, tools: ToolContext, case: SupportCase) -> None:
    """The confirmed cancel. Nothing unsent survives a cancelled case."""
    live = await drafts.live_email(ctx.session, case)
    maybe_sent = live is not None and live.status is OutboundEmailStatus.SENDING
    if live is not None and maybe_sent:
        # A send that gave up after claiming: it may have gone out. Never discard
        # that silently; record that the outcome is unknown.
        await drafts.mark_needs_attention(ctx.session, live, now=ctx.now)
    email = await drafts.discard_live(ctx.session, case, to_status=OutboundEmailStatus.CANCELLED)
    if email is not None:
        await retire_buttons(tools, email)
    await transition(
        ctx.session,
        case,
        CaseStatus.CANCELLED,
        reason="user cancelled the case",
        actor=TransitionActor.USER,
        event_id=ctx.event.id,
    )
    if live is not None and maybe_sent:
        await tools.outbox.send_message(CANCELLED_MAYBE_SENT_REPLY.format(to=live.to_address))
        return
    merchant = f"{case.merchant_name} support" if case.merchant_name else "support"
    await tools.outbox.send_message(CANCELLED_REPLY.format(merchant=merchant))


def _payload_email_id(action: PendingAction) -> uuid.UUID:
    try:
        return uuid.UUID(str(action.payload["outbound_email_id"]))
    except (KeyError, ValueError):
        raise PermanentEventError(f"action {action.id} has an invalid email payload") from None


async def _action_case(ctx: HandlerContext, action: PendingAction) -> SupportCase | None:
    if action.case_id is None:
        return None
    return await case_service.get_case(ctx.session, action.case_id, user_id=ctx.event.user_id)


def _require(case: SupportCase | None, action: PendingAction) -> SupportCase:
    if case is None:
        raise PermanentEventError(f"a {action.kind.value} action must name its case")
    return case

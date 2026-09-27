"""Handlers for what the user does in chat: messages and button presses.

M3 version: a message is echoed back with a test button, which proves the
round trip through the queue, `pending_actions` and Telegram. M5 replaces the
echo with the intake agent. The button handler stays; it gains one branch per
`ActionKind`.
"""

from datetime import timedelta
from typing import assert_never

from app.actions import service as action_service
from app.actions.models import ActionKind, PendingAction
from app.actions.service import ButtonSpec
from app.events.handlers import HandlerContext
from app.events.schemas import ButtonPressPayload, UserMessagePayload
from app.telegram.delivery import TelegramOutbox

TEST_BUTTON_LIFETIME = timedelta(hours=24)

NOT_TEXT_REPLY = "I can only read text messages for now."
STALE_BUTTON_REPLY = "This button is no longer valid."


async def handle_user_message(ctx: HandlerContext, payload: UserMessagePayload) -> None:
    outbox = await TelegramOutbox.for_event(ctx)
    if payload.text is None or not payload.text.strip():
        await outbox.send_message(NOT_TEXT_REPLY)
        return
    group_id = await action_service.create_group(
        ctx.session,
        user_id=ctx.event.user_id,
        buttons=[ButtonSpec(ActionKind.ECHO_TEST, "Test button")],
        expires_at=ctx.now + TEST_BUTTON_LIFETIME,
    )
    await outbox.send_message(f"You said: {payload.text}", action_group_id=group_id)


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
    await _run_action(outbox, result.action)


async def _run_action(outbox: TelegramOutbox, action: PendingAction) -> None:
    match action.kind:
        case ActionKind.ECHO_TEST:
            await outbox.send_message(f'You pressed "{action.label}". Buttons work.')
        case _:
            # mypy flags any ActionKind without a branch.
            assert_never(action.kind)

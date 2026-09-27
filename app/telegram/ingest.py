"""Turn a raw Telegram update into a queued event.

The one entry point for inbound Telegram traffic: the webhook and the local
poller both call `ingest_update`. It authorizes the sender, normalizes the
update and enqueues it. It never calls the LLM or contains workflow logic
(PLAN D1).

Deduplication keys (PLAN D1) come from ids Telegram never reuses:
`message:{chat_id}:{message_id}` for messages and `callback:{id}` for button
presses. `update_id` is avoided because Telegram restarts it at a random value
after a week without updates.
"""

from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import service as event_service
from app.events.models import EventSource, EventType
from app.events.schemas import ButtonPressPayload, NewEvent, UserMessagePayload
from app.logging import get_logger
from app.telegram.keyboards import decode_callback_data
from app.telegram.schemas import TelegramChat, TelegramUpdate, TelegramUser
from app.users.service import get_or_create_user

log = get_logger(__name__)

# The update kinds the bot asks Telegram for; everything else is dropped.
ALLOWED_UPDATES = ["message", "callback_query"]


async def ingest_update(
    session: AsyncSession, raw: dict[str, Any], *, allowed_user_id: int | None
) -> int | None:
    """Enqueue the update's event and return its id.

    Returns None when the update is dropped (unauthorized, unsupported,
    malformed) or is a duplicate. Only ids are logged, never content.
    """
    try:
        update = TelegramUpdate.model_validate(raw)
    except ValidationError:
        log.warning("telegram_update_invalid")
        return None

    sender: TelegramUser | None
    chat: TelegramChat | None
    event_type: EventType
    external_id: str
    payload: UserMessagePayload | ButtonPressPayload
    if update.message is not None:
        message = update.message
        sender, chat = message.from_user, message.chat
        event_type = EventType.USER_MESSAGE
        external_id = f"message:{message.chat.id}:{message.message_id}"
        payload = UserMessagePayload(text=message.text, telegram_message_id=message.message_id)
    elif update.callback_query is not None:
        query = update.callback_query
        sender = query.from_user
        chat = query.message.chat if query.message is not None else None
        event_type = EventType.USER_BUTTON_ACTION
        external_id = f"callback:{query.id}"
        payload = ButtonPressPayload(
            action_id=decode_callback_data(query.data),
            callback_query_id=query.id,
            telegram_message_id=query.message.message_id if query.message else None,
        )
    else:
        kinds = sorted(k for k in raw if k != "update_id")
        log.info("telegram_update_ignored", update_id=update.update_id, kinds=kinds)
        return None

    authorized = _authorized_sender(update.update_id, sender, chat, allowed_user_id)
    if authorized is None:
        return None
    user = await get_or_create_user(session, authorized.id, display_name=authorized.full_name)
    new_event = NewEvent(
        user_id=user.id,
        type=event_type,
        source=EventSource.TELEGRAM,
        external_id=external_id,
        payload=payload.model_dump(mode="json"),
    )
    return await event_service.enqueue(session, new_event)


def _authorized_sender(
    update_id: int,
    sender: TelegramUser | None,
    chat: TelegramChat | None,
    allowed_user_id: int | None,
) -> TelegramUser | None:
    """The sender, if they may use the bot here; otherwise log why not and return None."""
    reason: str | None = None
    if sender is None or sender.is_bot:
        reason = "no_human_sender"
    elif allowed_user_id is None:
        reason = "no_allowed_user_configured"
    elif sender.id != allowed_user_id:
        reason = "sender_not_allowed"
    elif chat is None or chat.type != "private":
        reason = "not_a_private_chat"
    if reason is None:
        return sender
    # The sender id is logged (never content) so the owner can find their id
    # during setup, when TELEGRAM_ALLOWED_USER_ID is still unset.
    log.warning(
        "telegram_update_rejected",
        update_id=update_id,
        reason=reason,
        sender_id=sender.id if sender else None,
        chat_type=chat.type if chat else None,
    )
    return None

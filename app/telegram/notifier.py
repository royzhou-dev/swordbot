"""Tells the user in Telegram when an event is given up on (PLAN D1)."""

from app.db.session import Database
from app.events.models import EventType
from app.events.schemas import ClaimedEvent
from app.events.worker import LoggingDeadEventNotifier
from app.logging import get_logger
from app.telegram.delivery import SendMessageOp, TelegramOutbox
from app.users.models import User

log = get_logger(__name__)


class TelegramDeadEventNotifier:
    """Logs the dead event and queues a short notice to its user.

    The notice goes through the queue like any reply, so it is delivered once
    Telegram is reachable again. A notice that itself fails is only logged, so
    failures can't cascade into more notices. Tidy-up calls (acknowledging a
    press, removing buttons) are not worth a notice either.
    """

    def __init__(self, database: Database) -> None:
        self._database = database
        self._log_only = LoggingDeadEventNotifier()

    async def event_dead(self, event: ClaimedEvent, error_type: str) -> None:
        await self._log_only.event_dead(event, error_type)
        if event.type is EventType.TELEGRAM_OUTBOUND and not _is_reply(event):
            return
        async with self._database.transaction() as session:
            user = await session.get(User, event.user_id)
            if user is None:
                return
            outbox = TelegramOutbox(
                session,
                user_id=user.id,
                chat_id=user.telegram_user_id,
                key_prefix=f"dead:{event.id}",
            )
            await outbox.send_message(
                "Sorry, something went wrong on my side and I had to give up on one step "
                f"(reference #{event.id}). You may need to repeat your last message.",
                failure_notice=True,
            )
        log.info("event_dead_notice_queued", event_id=event.id)


def _is_reply(event: ClaimedEvent) -> bool:
    """Whether a dead outbound event was a real message, other than a failure notice."""
    if event.payload.get("op") != "send_message":
        return False
    try:
        return not SendMessageOp.model_validate(event.payload).failure_notice
    except ValueError:
        return False

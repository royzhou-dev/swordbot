"""Which handler runs for each event type."""

from zoneinfo import ZoneInfo

from app.agent.drafting import DraftingAgent
from app.agent.intake import IntakeAgent
from app.chat.handlers import UserMessageHandler, handle_button_press
from app.db.session import Database
from app.email.gmail_client import GmailClient
from app.email.sending import EmailSender
from app.events.handlers import HandlerRegistry
from app.events.models import EventType
from app.events.schemas import (
    ButtonPressPayload,
    DraftEmailPayload,
    SendEmailPayload,
    UserMessagePayload,
)
from app.llm.client import LLMClient
from app.telegram.client import TelegramClient
from app.telegram.delivery import TelegramDelivery, TelegramOutboundPayload


def build_registry(
    telegram: TelegramClient,
    llm: LLMClient,
    *,
    gmail: GmailClient,
    database: Database,
    user_timezone: ZoneInfo,
    sender_address: str | None = None,
) -> HandlerRegistry:
    """`database` lets the send handler commit its claim on its own (PLAN D14)."""
    drafting = DraftingAgent(llm, timezone=user_timezone)
    registry = HandlerRegistry()
    registry.register(
        EventType.USER_MESSAGE,
        UserMessagePayload,
        UserMessageHandler(IntakeAgent(llm, timezone=user_timezone), drafting),
    )
    registry.register(EventType.USER_BUTTON_ACTION, ButtonPressPayload, handle_button_press)
    registry.register(EventType.DRAFT_EMAIL, DraftEmailPayload, drafting.draft)
    registry.register(
        EventType.SEND_EMAIL,
        SendEmailPayload,
        EmailSender(database, gmail, sender_address=sender_address),
    )
    registry.register(
        EventType.TELEGRAM_OUTBOUND, TelegramOutboundPayload, TelegramDelivery(telegram)
    )
    return registry

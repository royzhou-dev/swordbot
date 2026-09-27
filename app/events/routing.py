"""Which handler runs for each event type."""

from zoneinfo import ZoneInfo

from app.agent.drafting import DraftingAgent
from app.agent.intake import IntakeAgent
from app.chat.handlers import UserMessageHandler, handle_button_press
from app.events.handlers import HandlerRegistry
from app.events.models import EventType
from app.events.schemas import ButtonPressPayload, DraftEmailPayload, UserMessagePayload
from app.llm.client import LLMClient
from app.telegram.client import TelegramClient
from app.telegram.delivery import TelegramDelivery, TelegramOutboundPayload


def build_registry(
    telegram: TelegramClient, llm: LLMClient, *, user_timezone: ZoneInfo
) -> HandlerRegistry:
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
        EventType.TELEGRAM_OUTBOUND, TelegramOutboundPayload, TelegramDelivery(telegram)
    )
    return registry

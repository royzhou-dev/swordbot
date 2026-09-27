"""Which handler runs for each event type."""

from app.chat.handlers import handle_button_press, handle_user_message
from app.events.handlers import HandlerRegistry
from app.events.models import EventType
from app.events.schemas import ButtonPressPayload, UserMessagePayload
from app.telegram.client import TelegramClient
from app.telegram.delivery import TelegramDelivery, TelegramOutboundPayload


def build_registry(telegram: TelegramClient) -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register(EventType.USER_MESSAGE, UserMessagePayload, handle_user_message)
    registry.register(EventType.USER_BUTTON_ACTION, ButtonPressPayload, handle_button_press)
    registry.register(
        EventType.TELEGRAM_OUTBOUND, TelegramOutboundPayload, TelegramDelivery(telegram)
    )
    return registry

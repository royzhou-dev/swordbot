"""Pydantic models for the parts of the Telegram Bot API we use.

Only the fields the app reads are declared; everything else is ignored, so new
Telegram fields never break parsing.
"""

from pydantic import BaseModel, ConfigDict, Field


class _TelegramModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class TelegramUser(_TelegramModel):
    id: int
    is_bot: bool = False
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None

    @property
    def full_name(self) -> str | None:
        """The name as Telegram shows it: first and last name."""
        parts = [p.strip() for p in (self.first_name, self.last_name) if p and p.strip()]
        return " ".join(parts) or None


class TelegramChat(_TelegramModel):
    id: int
    # "private", "group", "supergroup" or "channel".
    type: str


class TelegramMessage(_TelegramModel):
    message_id: int
    chat: TelegramChat
    # Absent for channel posts.
    from_user: TelegramUser | None = Field(default=None, alias="from")
    # None for photos, stickers, voice notes and other non-text messages.
    text: str | None = None


class TelegramCallbackQuery(_TelegramModel):
    id: str
    from_user: TelegramUser = Field(alias="from")
    # The message the button was on. Very old messages may be missing.
    message: TelegramMessage | None = None
    data: str | None = None


class TelegramUpdate(_TelegramModel):
    update_id: int
    message: TelegramMessage | None = None
    callback_query: TelegramCallbackQuery | None = None


class SentMessage(_TelegramModel):
    message_id: int
    chat: TelegramChat

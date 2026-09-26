from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The person the assistant works for. v1 has exactly one, but every row is scoped by user."""

    __tablename__ = "users"

    # Telegram user ids can exceed 32 bits.
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    # Name used to sign outbound support emails (M6).
    signature_name: Mapped[str | None] = mapped_column(String(200))

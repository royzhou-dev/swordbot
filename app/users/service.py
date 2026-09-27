from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.users.models import User

MAX_NAME_LENGTH = 200


async def get_user_by_telegram_id(session: AsyncSession, telegram_user_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))


async def get_or_create_user(
    session: AsyncSession, telegram_user_id: int, *, display_name: str | None = None
) -> User:
    """Return the user with this Telegram id, creating it if needed. Does not commit.

    `display_name` is the user's current Telegram name. When given, it is kept
    up to date, because it signs outbound emails by default.

    v1 has one user, so two concurrent first-time creations can't happen in
    practice; if they did, the unique index would fail the second one.
    """
    name = display_name[:MAX_NAME_LENGTH] if display_name else None
    user = await get_user_by_telegram_id(session, telegram_user_id)
    if user is None:
        user = User(telegram_user_id=telegram_user_id, display_name=name)
        session.add(user)
        await session.flush()
    elif name is not None and user.display_name != name:
        user.display_name = name
        await session.flush()
    return user


def default_signature_name(user: User) -> str | None:
    """The name that signs emails unless a case says otherwise.

    `signature_name` (no way to set it yet; reserved for /settings) wins over
    the Telegram name.
    """
    return user.signature_name or user.display_name

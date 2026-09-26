from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.users.models import User


async def get_user_by_telegram_id(session: AsyncSession, telegram_user_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))


async def get_or_create_user(session: AsyncSession, telegram_user_id: int) -> User:
    """Return the user with this Telegram id, creating it if needed. Does not commit.

    v1 has one user, so two concurrent first-time creations can't happen in
    practice; if they did, the unique index would fail the second one.
    """
    user = await get_user_by_telegram_id(session, telegram_user_id)
    if user is None:
        user = User(telegram_user_id=telegram_user_id)
        session.add(user)
        await session.flush()
    return user

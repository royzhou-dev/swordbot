"""The chat log kept per case as LLM context (`case_messages`).

Functions take the caller's session and never commit.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.models import CaseMessage, MessageRole, SupportCase


async def add_message(
    session: AsyncSession,
    case: SupportCase,
    role: MessageRole,
    text: str,
    *,
    event_id: int | None,
    telegram_message_id: int | None = None,
) -> CaseMessage:
    message = CaseMessage(
        case_id=case.id,
        user_id=case.user_id,
        role=role,
        text=text,
        telegram_message_id=telegram_message_id,
        event_id=event_id,
    )
    session.add(message)
    await session.flush()
    return message


async def recent_messages(
    session: AsyncSession, case: SupportCase, *, limit: int
) -> list[CaseMessage]:
    """The case's last `limit` messages, oldest first."""
    rows = await session.scalars(
        select(CaseMessage)
        .where(CaseMessage.case_id == case.id)
        .order_by(CaseMessage.id.desc())
        .limit(limit)
    )
    return list(reversed(rows.all()))

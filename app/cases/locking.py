from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.cases.errors import ConcurrentCaseUpdateError
from app.cases.models import SupportCase


async def flush_case(session: AsyncSession, case: SupportCase) -> None:
    """Flush pending changes, turning an optimistic-lock conflict into a typed error.

    After a conflict the session must be rolled back; the caller's unit of work
    should reload the case and start over.
    """
    # Read the id first: a failed flush expires the object, and it can't be reloaded.
    case_id = case.id
    try:
        await session.flush()
    except StaleDataError as exc:
        raise ConcurrentCaseUpdateError(case_id) from exc

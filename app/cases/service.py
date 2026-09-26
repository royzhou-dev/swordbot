"""Case operations: create, load, update operational fields, and record facts.

Every function takes the caller's session and never commits.
"""

import json
import math
import re
import uuid
from datetime import date, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.errors import CaseClosedError, CaseNotFoundError, InvalidFactError
from app.cases.locking import flush_case
from app.cases.models import (
    CaseFact,
    CaseTransition,
    FactSource,
    SupportCase,
    TransitionActor,
)
from app.cases.schemas import CaseFieldsUpdate
from app.cases.state_machine import CLOSED_STATUSES, INITIAL_STATUS
from app.logging import get_logger

log = get_logger(__name__)

# Fact keys mirrored into a `SupportCase` column. `set_fact` is the only writer
# of these columns, so each column always equals the latest fact for its key.
FACT_COLUMNS: Final = frozenset(
    {
        "merchant_name",
        "merchant_domain",
        "issue_type",
        "issue_summary",
        "desired_resolution",
        "order_number",
        "order_date",
        "support_email",
    }
)

_FACT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


async def create_case(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    actor: TransitionActor,
    reason: str = "case created",
    event_id: uuid.UUID | None = None,
) -> SupportCase:
    """Create a case in the initial status and log its creation as the first transition."""
    case = SupportCase(id=uuid.uuid4(), user_id=user_id, status=INITIAL_STATUS)
    session.add(case)
    await flush_case(session, case)
    session.add(
        CaseTransition(
            case_id=case.id,
            user_id=user_id,
            from_status=None,
            to_status=INITIAL_STATUS,
            reason=reason,
            actor=actor,
            event_id=event_id,
        )
    )
    await session.flush()
    log.info("case_created", case_id=str(case.id), user_id=str(user_id))
    return case


async def get_case(session: AsyncSession, case_id: uuid.UUID, *, user_id: uuid.UUID) -> SupportCase:
    """Load a case owned by `user_id`. Another user's case is reported as not found."""
    case = await session.scalar(
        select(SupportCase).where(SupportCase.id == case_id, SupportCase.user_id == user_id)
    )
    if case is None:
        raise CaseNotFoundError(case_id)
    return case


async def get_focused_case(session: AsyncSession, user_id: uuid.UUID) -> SupportCase | None:
    return await session.scalar(
        select(SupportCase).where(SupportCase.user_id == user_id, SupportCase.focused.is_(True))
    )


async def focus_case(session: AsyncSession, case: SupportCase) -> None:
    """Make `case` the user's focused case, unfocusing any other (PLAN D7)."""
    if case.status in CLOSED_STATUSES:
        raise CaseClosedError(case.id, case.status)
    if case.focused:
        return
    others = await session.scalars(
        select(SupportCase).where(
            SupportCase.user_id == case.user_id,
            SupportCase.focused.is_(True),
            SupportCase.id != case.id,
        )
    )
    for other in others:
        other.focused = False
        # Flush the unfocus first so the one-focused-per-user index never sees two.
        await flush_case(session, other)
    case.focused = True
    await flush_case(session, case)


async def update_fields(session: AsyncSession, case: SupportCase, update: CaseFieldsUpdate) -> None:
    """Apply the fields explicitly set on `update`."""
    for name in update.model_fields_set:
        setattr(case, name, getattr(update, name))
    await flush_case(session, case)


def _validate_column_fact(key: str, value: object) -> object:
    """Check a value for a fact-backed column and return its JSON form."""
    if value is None:
        return None
    if key == "order_date":
        if not isinstance(value, date) or isinstance(value, datetime):
            raise InvalidFactError("order_date must be a date")
        return value.isoformat()
    if not isinstance(value, str) or not value.strip():
        raise InvalidFactError(f"{key} must be a non-empty string")
    max_length = getattr(SupportCase.__table__.c[key].type, "length", None)
    if max_length is not None and len(value) > max_length:
        raise InvalidFactError(f"{key} is longer than {max_length} characters")
    return value


def _validate_json_fact(value: object) -> object:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidFactError("fact value must be JSON-serializable") from exc
    return value


async def set_fact(
    session: AsyncSession,
    case: SupportCase,
    key: str,
    value: object,
    *,
    source: FactSource,
    source_ref: str | None = None,
    confidence: float | None = None,
) -> CaseFact:
    """Record a fact with its provenance (Invariant 1). `None` clears the fact.

    Facts are append-only. For a fact-backed key the matching case column is
    updated in the same flush.
    """
    if not _FACT_KEY.match(key):
        raise InvalidFactError(f"invalid fact key {key!r}")
    if confidence is not None and not (math.isfinite(confidence) and 0.0 <= confidence <= 1.0):
        raise InvalidFactError("confidence must be between 0 and 1")

    if key in FACT_COLUMNS:
        json_value = _validate_column_fact(key, value)
        setattr(case, key, value)
    else:
        json_value = _validate_json_fact(value)

    fact = CaseFact(
        case_id=case.id,
        user_id=case.user_id,
        key=key,
        value=json_value,
        source=source,
        source_ref=source_ref,
        confidence=confidence,
    )
    session.add(fact)
    await flush_case(session, case)
    # The value is not logged: facts can contain personal or order details.
    log.info("case_fact_set", case_id=str(case.id), key=key, source=source.value)
    return fact


async def get_current_facts(session: AsyncSession, case: SupportCase) -> dict[str, CaseFact]:
    """The latest fact per key, leaving out keys whose latest value was cleared."""
    rows = await session.scalars(
        select(CaseFact).where(CaseFact.case_id == case.id).order_by(CaseFact.id)
    )
    latest: dict[str, CaseFact] = {}
    for fact in rows:
        latest[fact.key] = fact
    return {key: fact for key, fact in latest.items() if fact.value is not None}


async def list_transitions(session: AsyncSession, case: SupportCase) -> list[CaseTransition]:
    rows = await session.scalars(
        select(CaseTransition).where(CaseTransition.case_id == case.id).order_by(CaseTransition.id)
    )
    return list(rows)

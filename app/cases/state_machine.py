"""The support case state machine.

`ALLOWED_TRANSITIONS` is the single source of truth for which status changes
are legal, and `transition` is the only code that changes `SupportCase.status`.
The LLM may recommend a transition; this module decides.

This module checks only that a move is legal. Business preconditions, such as
needing an approval record before `READY_TO_SEND` (PLAN D2), belong to the
caller that requests the move.
"""

import uuid
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.errors import InvalidTransitionError
from app.cases.locking import flush_case
from app.cases.models import CaseStatus, CaseTransition, SupportCase, TransitionActor
from app.db.base import utcnow
from app.logging import get_logger

S = CaseStatus

# Closed cases take no new work. RESOLVED can be reopened; CANCELLED is terminal.
CLOSED_STATUSES: frozenset[CaseStatus] = frozenset({S.RESOLVED, S.CANCELLED})

# Statuses in which the workflow is running. Any of them may move to CANCELLED
# (the user gives up; nothing is sent to support) or ERROR (needs attention).
ACTIVE_STATUSES: frozenset[CaseStatus] = frozenset(CaseStatus) - CLOSED_STATUSES - {S.ERROR}

_WORKFLOW: dict[CaseStatus, set[CaseStatus]] = {
    S.GATHERING_CONTEXT: {S.READY_TO_DRAFT},
    S.READY_TO_DRAFT: {S.WAITING_FOR_USER_APPROVAL, S.GATHERING_CONTEXT},
    # Send pressed, redraft requested, or more information needed.
    S.WAITING_FOR_USER_APPROVAL: {S.READY_TO_SEND, S.READY_TO_DRAFT, S.GATHERING_CONTEXT},
    # Sent, or the send failed / needs attention and goes back to the user.
    S.READY_TO_SEND: {S.WAITING_FOR_SUPPORT, S.WAITING_FOR_USER_APPROVAL},
    # A reply arrived, a follow-up is due (M14), or the user reports it resolved.
    S.WAITING_FOR_SUPPORT: {S.PROCESSING_SUPPORT_REPLY, S.READY_TO_DRAFT, S.RESOLVED},
    # Ask the user, reply, keep waiting (e.g. an auto-acknowledgement), or resolved.
    S.PROCESSING_SUPPORT_REPLY: {
        S.WAITING_FOR_USER,
        S.READY_TO_REPLY,
        S.WAITING_FOR_SUPPORT,
        S.RESOLVED,
    },
    S.WAITING_FOR_USER: {S.READY_TO_REPLY, S.WAITING_FOR_SUPPORT, S.RESOLVED},
    # Draft a reply for approval, or a routine auto-reply was sent (M12).
    S.READY_TO_REPLY: {S.WAITING_FOR_USER_APPROVAL, S.WAITING_FOR_SUPPORT},
    # Reopen: support wrote again on the thread, or the user reopened the case.
    S.RESOLVED: {S.PROCESSING_SUPPORT_REPLY, S.WAITING_FOR_USER},
    S.CANCELLED: set(),
    # Recovery is explicit and must give a reason.
    S.ERROR: set(ACTIVE_STATUSES) | {S.CANCELLED},
}


def _build_allowed() -> Mapping[CaseStatus, frozenset[CaseStatus]]:
    allowed: dict[CaseStatus, frozenset[CaseStatus]] = {}
    for status in CaseStatus:
        targets = set(_WORKFLOW[status])
        if status in ACTIVE_STATUSES:
            targets |= {S.CANCELLED, S.ERROR}
        targets.discard(status)
        allowed[status] = frozenset(targets)
    return allowed


ALLOWED_TRANSITIONS: Mapping[CaseStatus, frozenset[CaseStatus]] = _build_allowed()

INITIAL_STATUS = S.GATHERING_CONTEXT


def is_allowed(from_status: CaseStatus, to_status: CaseStatus) -> bool:
    return to_status in ALLOWED_TRANSITIONS[from_status]


async def transition(
    session: AsyncSession,
    case: SupportCase,
    to_status: CaseStatus,
    *,
    reason: str,
    actor: TransitionActor,
    event_id: uuid.UUID | None = None,
) -> CaseTransition:
    """Move `case` to `to_status`, record the audit row, and bump `version`.

    Raises `InvalidTransitionError` for an illegal move and
    `ConcurrentCaseUpdateError` if the case changed since it was loaded.
    Does not commit.
    """
    if not reason.strip():
        raise ValueError("a transition needs a reason")
    from_status = case.status
    if not is_allowed(from_status, to_status):
        raise InvalidTransitionError(case.id, from_status, to_status)

    case.status = to_status
    if to_status is S.RESOLVED:
        case.resolved_at = utcnow()
    elif from_status is S.RESOLVED:
        case.resolved_at = None
    if to_status in CLOSED_STATUSES:
        case.focused = False

    record = CaseTransition(
        case_id=case.id,
        user_id=case.user_id,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        actor=actor,
        event_id=event_id,
    )
    session.add(record)
    await flush_case(session, case)

    # The reason is not logged: it may be derived from user or LLM text.
    get_logger(__name__).info(
        "case_transition",
        case_id=str(case.id),
        user_id=str(case.user_id),
        from_status=from_status.value,
        to_status=to_status.value,
        actor=actor.value,
        event_id=str(event_id) if event_id else None,
    )
    return record

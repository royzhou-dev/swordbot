"""Which case, and which part of the workflow, a chat message belongs to (PLAN D7).

v1 routes by the user's focused case and its status:

- gathering context or ready to draft: intake (corrections are welcome);
- waiting for approval of a draft: draft review (M6);
- approved (READY_TO_SEND): handled in code, no LLM; it settles whatever the
  send left unfinished (`app.email.sending.resume_unfinished_send`);
- sent, waiting for support (M7): intake with no case, told about the sent
  case (`Route.sent`). A question about it gets a status reply and opens
  nothing; a different problem opens a new case, as below;
- anything else, or no focused case: intake with no case, which opens a new
  one if the message describes a problem.

Multi-case routing arrives in M13.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.models import CaseStatus, SupportCase
from app.cases.service import get_focused_case

INTAKE_STATUSES: Final = frozenset({CaseStatus.GATHERING_CONTEXT, CaseStatus.READY_TO_DRAFT})


class Stage(StrEnum):
    INTAKE = "intake"
    DRAFT_REVIEW = "draft_review"
    APPROVED = "approved"


@dataclass(frozen=True, slots=True)
class Route:
    stage: Stage
    # None only for INTAKE, when there is no case for the message yet.
    case: SupportCase | None
    # With no case: the focused case whose email went out, if any. The
    # message may be about it rather than a new problem.
    sent: SupportCase | None = None


async def route_message(session: AsyncSession, user_id: uuid.UUID) -> Route:
    case = await get_focused_case(session, user_id)
    if case is None:
        return Route(Stage.INTAKE, None)
    if case.status in INTAKE_STATUSES:
        return Route(Stage.INTAKE, case)
    if case.status is CaseStatus.WAITING_FOR_USER_APPROVAL:
        return Route(Stage.DRAFT_REVIEW, case)
    if case.status is CaseStatus.READY_TO_SEND:
        return Route(Stage.APPROVED, case)
    if case.status is CaseStatus.WAITING_FOR_SUPPORT:
        return Route(Stage.INTAKE, None, sent=case)
    return Route(Stage.INTAKE, None)

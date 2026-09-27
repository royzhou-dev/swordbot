"""Creating, validating and consuming button actions (PLAN D3).

Functions take the caller's session and never commit.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.models import ActionKind, ActionStatus, PendingAction
from app.cases.models import CaseStatus, SupportCase
from app.db.session import rowcount
from app.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ButtonSpec:
    kind: ActionKind
    label: str
    payload: dict[str, Any] = field(default_factory=dict)


class ConsumeOutcome(StrEnum):
    ACCEPTED = "accepted"
    # Unknown id, or another user's action. Deliberately indistinguishable.
    NOT_FOUND = "not_found"
    # Already pressed, or closed because another button in its group was.
    ALREADY_USED = "already_used"
    EXPIRED = "expired"
    # The case has moved on since the button was shown.
    WRONG_CASE_STATUS = "wrong_case_status"


@dataclass(frozen=True, slots=True)
class ConsumeResult:
    outcome: ConsumeOutcome
    # The action, when it exists and belongs to the user.
    action: PendingAction | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome is ConsumeOutcome.ACCEPTED


async def create_group(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    buttons: list[ButtonSpec],
    case_id: uuid.UUID | None = None,
    expected_case_status: CaseStatus | None = None,
    expires_at: datetime | None = None,
) -> uuid.UUID:
    """Create one open action per button, all in a new group. Returns the group id."""
    if not buttons:
        raise ValueError("a group needs at least one button")
    if (case_id is None) != (expected_case_status is None):
        raise ValueError("case_id and expected_case_status go together")
    group_id = uuid.uuid4()
    for position, button in enumerate(buttons):
        session.add(
            PendingAction(
                id=uuid.uuid4(),
                user_id=user_id,
                case_id=case_id,
                group_id=group_id,
                position=position,
                kind=button.kind,
                label=button.label,
                payload=button.payload,
                status=ActionStatus.OPEN,
                expected_case_status=expected_case_status,
                expires_at=expires_at,
            )
        )
    await session.flush()
    return group_id


async def open_actions_in_group(
    session: AsyncSession, group_id: uuid.UUID, *, user_id: uuid.UUID, now: datetime
) -> list[PendingAction]:
    """The group's buttons that can still be pressed, in display order."""
    rows = await session.scalars(
        select(PendingAction)
        .where(
            PendingAction.group_id == group_id,
            PendingAction.user_id == user_id,
            PendingAction.status == ActionStatus.OPEN,
        )
        .order_by(PendingAction.position)
    )
    return [a for a in rows.all() if a.expires_at is None or a.expires_at > now]


async def record_delivery(
    session: AsyncSession, group_id: uuid.UUID, *, chat_id: int, message_id: int
) -> None:
    """Remember which Telegram message shows the group's buttons."""
    await session.execute(
        update(PendingAction)
        .where(PendingAction.group_id == group_id)
        .values(telegram_chat_id=chat_id, telegram_message_id=message_id)
        .execution_options(synchronize_session=False)
    )


async def group_message_id(session: AsyncSession, group_id: uuid.UUID) -> int | None:
    """The Telegram message showing the group's buttons, or None if not delivered yet."""
    return await session.scalar(
        select(PendingAction.telegram_message_id)
        .where(PendingAction.group_id == group_id, PendingAction.telegram_message_id.is_not(None))
        .limit(1)
    )


async def has_open_actions(
    session: AsyncSession, group_id: uuid.UUID, *, user_id: uuid.UUID, now: datetime
) -> bool:
    return bool(await open_actions_in_group(session, group_id, user_id=user_id, now=now))


async def supersede_group(session: AsyncSession, group_id: uuid.UUID, *, now: datetime) -> None:
    """Close every open action in the group, e.g. because the prompt no longer applies."""
    await session.execute(
        update(PendingAction)
        .where(PendingAction.group_id == group_id, PendingAction.status == ActionStatus.OPEN)
        .values(status=ActionStatus.SUPERSEDED, updated_at=now)
        .execution_options(synchronize_session=False)
    )


async def consume(
    session: AsyncSession,
    action_id: uuid.UUID | None,
    *,
    user_id: uuid.UUID,
    event_id: int,
    now: datetime,
) -> ConsumeResult:
    """Validate a button press and consume the action, closing the rest of its group.

    Only an `ACCEPTED` result authorizes the action. Consumption is a
    conditional UPDATE, so a duplicate press can never be accepted twice.
    """
    action = None if action_id is None else await session.get(PendingAction, action_id)
    if action is None or action.user_id != user_id:
        return _result(ConsumeOutcome.NOT_FOUND, action_id, None)
    await session.refresh(action)
    if action.status is not ActionStatus.OPEN:
        return _result(ConsumeOutcome.ALREADY_USED, action_id, action)
    if action.expires_at is not None and action.expires_at <= now:
        await supersede_group(session, action.group_id, now=now)
        return _result(ConsumeOutcome.EXPIRED, action_id, action)
    if action.case_id is not None:
        case = await session.get(SupportCase, action.case_id)
        if case is None or case.status is not action.expected_case_status:
            await supersede_group(session, action.group_id, now=now)
            return _result(ConsumeOutcome.WRONG_CASE_STATUS, action_id, action)

    result = await session.execute(
        update(PendingAction)
        .where(PendingAction.id == action.id, PendingAction.status == ActionStatus.OPEN)
        .values(
            status=ActionStatus.CONSUMED,
            consumed_at=now,
            consumed_by_event_id=event_id,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if rowcount(result) == 0:
        return _result(ConsumeOutcome.ALREADY_USED, action_id, action)
    await supersede_group(session, action.group_id, now=now)
    await session.refresh(action)
    return _result(ConsumeOutcome.ACCEPTED, action_id, action)


def _result(
    outcome: ConsumeOutcome, action_id: uuid.UUID | None, action: PendingAction | None
) -> ConsumeResult:
    log.info(
        "action_press",
        action_id=str(action_id) if action_id else None,
        outcome=outcome.value,
        kind=action.kind.value if action else None,
    )
    return ConsumeResult(outcome, action)

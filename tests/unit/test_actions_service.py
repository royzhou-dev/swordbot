"""Button actions: creation, validation and one-time consumption (PLAN D3)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions import service
from app.actions.models import ActionKind, ActionStatus, PendingAction
from app.actions.service import ButtonSpec, ConsumeOutcome
from app.cases import service as case_service
from app.cases.models import CaseStatus, TransitionActor
from app.users.models import User

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
TWO_BUTTONS = [ButtonSpec(ActionKind.ECHO_TEST, "Yes"), ButtonSpec(ActionKind.ECHO_TEST, "No")]


async def _group(session: AsyncSession, user: User, **kwargs: object) -> list[PendingAction]:
    group_id = await service.create_group(
        session,
        user_id=user.id,
        buttons=TWO_BUTTONS,
        **kwargs,  # type: ignore[arg-type]
    )
    await session.commit()
    rows = await session.scalars(
        select(PendingAction)
        .where(PendingAction.group_id == group_id)
        .order_by(PendingAction.position)
    )
    return list(rows.all())


async def _consume(
    session: AsyncSession,
    action_id: uuid.UUID | None,
    user: User,
    event_id: int,
    now: datetime = NOW,
) -> ConsumeOutcome:
    result = await service.consume(session, action_id, user_id=user.id, event_id=event_id, now=now)
    await session.commit()
    return result.outcome


async def _status(session: AsyncSession, action: PendingAction) -> ActionStatus:
    await session.refresh(action)
    return action.status


async def test_group_needs_buttons(session: AsyncSession, user: User) -> None:
    with pytest.raises(ValueError, match="at least one"):
        await service.create_group(session, user_id=user.id, buttons=[])


async def test_case_and_expected_status_go_together(session: AsyncSession, user: User) -> None:
    with pytest.raises(ValueError, match="together"):
        await service.create_group(
            session, user_id=user.id, buttons=TWO_BUTTONS, case_id=uuid.uuid4()
        )


async def test_pressing_consumes_the_action_and_closes_its_siblings(
    session: AsyncSession, user: User, event_id: int
) -> None:
    yes, no = await _group(session, user)

    assert await _consume(session, yes.id, user, event_id) is ConsumeOutcome.ACCEPTED

    assert await _status(session, yes) is ActionStatus.CONSUMED
    assert yes.consumed_by_event_id == event_id
    assert yes.consumed_at == NOW
    assert await _status(session, no) is ActionStatus.SUPERSEDED


async def test_an_action_is_accepted_only_once(
    session: AsyncSession, user: User, event_id: int
) -> None:
    yes, no = await _group(session, user)
    assert await _consume(session, yes.id, user, event_id) is ConsumeOutcome.ACCEPTED
    assert await _consume(session, yes.id, user, event_id) is ConsumeOutcome.ALREADY_USED
    assert await _consume(session, no.id, user, event_id) is ConsumeOutcome.ALREADY_USED


async def test_unknown_or_missing_ids_are_not_found(
    session: AsyncSession, user: User, event_id: int
) -> None:
    assert await _consume(session, None, user, event_id) is ConsumeOutcome.NOT_FOUND
    assert await _consume(session, uuid.uuid4(), user, event_id) is ConsumeOutcome.NOT_FOUND


async def test_another_users_action_is_not_found_and_stays_open(
    session: AsyncSession, user: User, event_id: int
) -> None:
    other = User(telegram_user_id=3_000_000_003)
    session.add(other)
    await session.commit()
    theirs, _ = await _group(session, other)

    assert await _consume(session, theirs.id, user, event_id) is ConsumeOutcome.NOT_FOUND
    assert await _status(session, theirs) is ActionStatus.OPEN


async def test_expired_action_is_rejected_and_its_group_closed(
    session: AsyncSession, user: User, event_id: int
) -> None:
    yes, no = await _group(session, user, expires_at=NOW + timedelta(hours=1))

    later = NOW + timedelta(hours=1)
    assert await _consume(session, yes.id, user, event_id, later) is ConsumeOutcome.EXPIRED
    assert await _status(session, yes) is ActionStatus.SUPERSEDED
    assert await _status(session, no) is ActionStatus.SUPERSEDED


async def test_action_is_rejected_once_its_case_has_moved_on(
    session: AsyncSession, user: User, event_id: int
) -> None:
    case = await case_service.create_case(session, user_id=user.id, actor=TransitionActor.SYSTEM)
    await session.commit()
    # The case is gathering context, but the buttons were for a later status.
    yes, no = await _group(
        session,
        user,
        case_id=case.id,
        expected_case_status=CaseStatus.WAITING_FOR_USER_APPROVAL,
    )

    assert await _consume(session, yes.id, user, event_id) is ConsumeOutcome.WRONG_CASE_STATUS
    assert await _status(session, yes) is ActionStatus.SUPERSEDED
    assert await _status(session, no) is ActionStatus.SUPERSEDED


async def test_action_is_accepted_while_its_case_is_in_the_expected_status(
    session: AsyncSession, user: User, event_id: int
) -> None:
    case = await case_service.create_case(session, user_id=user.id, actor=TransitionActor.SYSTEM)
    await session.commit()
    yes, _ = await _group(
        session, user, case_id=case.id, expected_case_status=CaseStatus.GATHERING_CONTEXT
    )
    assert await _consume(session, yes.id, user, event_id) is ConsumeOutcome.ACCEPTED


async def test_open_actions_skip_expired_and_closed_ones(
    session: AsyncSession, user: User, event_id: int
) -> None:
    yes, _ = await _group(session, user, expires_at=NOW + timedelta(hours=1))
    group_id = yes.group_id

    open_now = await service.open_actions_in_group(session, group_id, user_id=user.id, now=NOW)
    assert [a.label for a in open_now] == ["Yes", "No"]

    later = NOW + timedelta(hours=2)
    assert await service.open_actions_in_group(session, group_id, user_id=user.id, now=later) == []

    await _consume(session, yes.id, user, event_id)
    assert await service.open_actions_in_group(session, group_id, user_id=user.id, now=NOW) == []


async def test_record_delivery_stores_the_message_on_every_button(
    session: AsyncSession, user: User
) -> None:
    yes, no = await _group(session, user)
    await service.record_delivery(session, yes.group_id, chat_id=5, message_id=77)
    await session.commit()
    for action in (yes, no):
        await session.refresh(action)
        assert (action.telegram_chat_id, action.telegram_message_id) == (5, 77)

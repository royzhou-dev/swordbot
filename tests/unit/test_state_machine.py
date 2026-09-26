import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases import service
from app.cases.errors import ConcurrentCaseUpdateError, InvalidTransitionError
from app.cases.models import CaseStatus, SupportCase, TransitionActor
from app.cases.state_machine import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    CLOSED_STATUSES,
    INITIAL_STATUS,
    is_allowed,
    transition,
)
from app.db.session import Database
from app.users.models import User

S = CaseStatus
S_AGENT = TransitionActor.AGENT

ALLOWED_PAIRS = [(a, b) for a in CaseStatus for b in sorted(ALLOWED_TRANSITIONS[a])]
DISALLOWED_PAIRS = [
    (a, b) for a in CaseStatus for b in CaseStatus if b not in ALLOWED_TRANSITIONS[a]
]


def _ids(pairs: list[tuple[CaseStatus, CaseStatus]]) -> list[str]:
    return [f"{a.value}->{b.value}" for a, b in pairs]


async def _case_in(session: AsyncSession, user: User, status: CaseStatus) -> SupportCase:
    case = await service.create_case(session, user_id=user.id, actor=TransitionActor.SYSTEM)
    # Test-only shortcut to put the case in an arbitrary starting status.
    case.status = status
    await session.commit()
    return case


# --- The table itself -------------------------------------------------------------


def test_every_status_has_an_entry() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(CaseStatus)


def test_no_self_transitions() -> None:
    for status, targets in ALLOWED_TRANSITIONS.items():
        assert status not in targets


def test_every_status_is_reachable_from_initial() -> None:
    seen = {INITIAL_STATUS}
    frontier = [INITIAL_STATUS]
    while frontier:
        for nxt in ALLOWED_TRANSITIONS[frontier.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    assert seen == set(CaseStatus)


def test_every_active_status_can_be_cancelled_or_errored() -> None:
    for status in ACTIVE_STATUSES:
        assert {S.CANCELLED, S.ERROR} <= ALLOWED_TRANSITIONS[status]


def test_cancelled_is_terminal() -> None:
    assert ALLOWED_TRANSITIONS[S.CANCELLED] == frozenset()


def test_cancel_while_waiting_for_support_is_allowed() -> None:
    assert is_allowed(S.WAITING_FOR_SUPPORT, S.CANCELLED)


def test_resolved_case_can_be_reopened() -> None:
    assert ALLOWED_TRANSITIONS[S.RESOLVED] == {S.PROCESSING_SUPPORT_REPLY, S.WAITING_FOR_USER}


def test_error_recovers_to_any_active_status_or_cancel() -> None:
    assert ALLOWED_TRANSITIONS[S.ERROR] == ACTIVE_STATUSES | {S.CANCELLED}


def test_ready_to_send_is_reached_only_from_approval_or_recovery() -> None:
    sources = {a for a, targets in ALLOWED_TRANSITIONS.items() if S.READY_TO_SEND in targets}
    assert sources == {S.WAITING_FOR_USER_APPROVAL, S.ERROR}


# --- transition() -----------------------------------------------------------------


@pytest.mark.parametrize(("from_status", "to_status"), ALLOWED_PAIRS, ids=_ids(ALLOWED_PAIRS))
async def test_allowed_transition_applies_and_is_logged(
    session: AsyncSession, user: User, from_status: CaseStatus, to_status: CaseStatus
) -> None:
    case = await _case_in(session, user, from_status)
    version_before = case.version
    event_id = uuid.uuid4()

    record = await transition(
        session, case, to_status, reason="test", actor=TransitionActor.AGENT, event_id=event_id
    )
    await session.commit()

    assert case.status is to_status
    assert case.version == version_before + 1
    assert (record.from_status, record.to_status) == (from_status, to_status)
    assert record.event_id == event_id
    assert record.actor is TransitionActor.AGENT
    history = await service.list_transitions(session, case)
    assert history[-1].id == record.id


@pytest.mark.parametrize(("from_status", "to_status"), DISALLOWED_PAIRS, ids=_ids(DISALLOWED_PAIRS))
async def test_disallowed_transition_raises_without_side_effects(
    from_status: CaseStatus, to_status: CaseStatus
) -> None:
    session = MagicMock()
    case = SupportCase(id=uuid.uuid4(), user_id=uuid.uuid4(), status=from_status, version=3)

    with pytest.raises(InvalidTransitionError) as exc_info:
        await transition(session, case, to_status, reason="test", actor=TransitionActor.AGENT)

    assert exc_info.value.from_status is from_status
    assert exc_info.value.to_status is to_status
    assert case.status is from_status
    assert case.version == 3
    session.add.assert_not_called()
    session.flush.assert_not_called()


async def test_disallowed_transition_leaves_stored_case_unchanged(
    session: AsyncSession, user: User
) -> None:
    case = await _case_in(session, user, S.GATHERING_CONTEXT)
    with pytest.raises(InvalidTransitionError):
        await transition(session, case, S.WAITING_FOR_SUPPORT, reason="x", actor=S_AGENT)
    await session.rollback()
    reloaded = await service.get_case(session, case.id, user_id=user.id)
    assert reloaded.status is S.GATHERING_CONTEXT
    assert len(await service.list_transitions(session, case)) == 1


async def test_blank_reason_is_rejected(session: AsyncSession, user: User) -> None:
    case = await _case_in(session, user, S.GATHERING_CONTEXT)
    with pytest.raises(ValueError, match="reason"):
        await transition(session, case, S.READY_TO_DRAFT, reason="  ", actor=S_AGENT)
    assert case.status is S.GATHERING_CONTEXT


async def test_resolving_sets_resolved_at_and_reopening_clears_it(
    session: AsyncSession, user: User
) -> None:
    case = await _case_in(session, user, S.WAITING_FOR_SUPPORT)
    await transition(session, case, S.RESOLVED, reason="refund confirmed", actor=S_AGENT)
    assert case.resolved_at is not None
    assert case.resolved_at.tzinfo is not None

    await transition(session, case, S.WAITING_FOR_USER, reason="user reopened", actor=S_AGENT)
    assert case.resolved_at is None


@pytest.mark.parametrize("closed", sorted(CLOSED_STATUSES))
async def test_closing_a_case_unfocuses_it(
    session: AsyncSession, user: User, closed: CaseStatus
) -> None:
    case = await _case_in(session, user, S.WAITING_FOR_SUPPORT)
    await service.focus_case(session, case)
    await transition(session, case, closed, reason="closed", actor=TransitionActor.USER)
    assert case.focused is False


async def test_concurrent_transition_is_rejected(database: Database, user: User) -> None:
    async with database.session_factory() as s:
        case = await _case_in(s, user, S.GATHERING_CONTEXT)

    async with database.session_factory() as a, database.session_factory() as b:
        case_a = await service.get_case(a, case.id, user_id=user.id)
        case_b = await service.get_case(b, case.id, user_id=user.id)

        await transition(a, case_a, S.READY_TO_DRAFT, reason="first", actor=S_AGENT)
        await a.commit()

        with pytest.raises(ConcurrentCaseUpdateError):
            await transition(b, case_b, S.CANCELLED, reason="second", actor=S_AGENT)
        await b.rollback()

    async with database.session_factory() as s:
        final = await service.get_case(s, case.id, user_id=user.id)
        assert final.status is S.READY_TO_DRAFT
        assert [t.reason for t in await service.list_transitions(s, final)] == [
            "case created",
            "first",
        ]

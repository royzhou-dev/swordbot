import uuid
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases import service
from app.cases.errors import CaseClosedError, CaseNotFoundError, InvalidFactError
from app.cases.models import CaseStatus, FactSource, SupportCase, TransitionActor
from app.cases.schemas import CaseFieldsUpdate
from app.cases.state_machine import transition
from app.db.session import Database
from app.users.models import User

SYSTEM = TransitionActor.SYSTEM


async def _new_case(session: AsyncSession, user: User) -> SupportCase:
    case = await service.create_case(session, user_id=user.id, actor=SYSTEM)
    await session.commit()
    return case


# --- create / get -------------------------------------------------------------


async def test_create_case_starts_gathering_context_with_audit_row(
    session: AsyncSession, user: User, event_id: int
) -> None:
    case = await service.create_case(
        session, user_id=user.id, actor=TransitionActor.USER, event_id=event_id
    )
    await session.commit()

    assert case.status is CaseStatus.GATHERING_CONTEXT
    assert case.version == 1
    assert case.focused is False
    assert case.auto_reply_enabled is False
    [created] = await service.list_transitions(session, case)
    assert created.from_status is None
    assert created.to_status is CaseStatus.GATHERING_CONTEXT
    assert created.event_id == event_id
    assert created.user_id == user.id


async def test_case_survives_a_new_session(database: Database, user: User) -> None:
    async with database.session_factory() as s:
        case = await _new_case(s, user)
        await service.set_fact(s, case, "merchant_name", "DoorDash", source=FactSource.USER_MESSAGE)
        await transition(s, case, CaseStatus.READY_TO_DRAFT, reason="ready", actor=SYSTEM)
        await s.commit()

    async with database.session_factory() as s:
        reloaded = await service.get_case(s, case.id, user_id=user.id)
        assert reloaded.status is CaseStatus.READY_TO_DRAFT
        assert reloaded.merchant_name == "DoorDash"
        assert reloaded.created_at.tzinfo is not None
        assert reloaded.updated_at.tzinfo is not None


async def test_get_case_hides_other_users_cases(session: AsyncSession, user: User) -> None:
    case = await _new_case(session, user)
    with pytest.raises(CaseNotFoundError):
        await service.get_case(session, case.id, user_id=uuid.uuid4())
    with pytest.raises(CaseNotFoundError):
        await service.get_case(session, uuid.uuid4(), user_id=user.id)


# --- facts ----------------------------------------------------------------------


async def test_set_fact_records_provenance_and_updates_column(
    session: AsyncSession, user: User
) -> None:
    case = await _new_case(session, user)
    fact = await service.set_fact(
        session,
        case,
        "order_number",
        "ABC123",
        source=FactSource.GMAIL_RECEIPT,
        source_ref="gmail:msg-1",
        confidence=0.9,
    )
    await session.commit()

    assert case.order_number == "ABC123"
    assert case.version == 2
    assert fact.value == "ABC123"
    assert fact.source is FactSource.GMAIL_RECEIPT
    assert fact.source_ref == "gmail:msg-1"
    assert fact.confidence == 0.9
    assert fact.user_id == user.id


async def test_latest_fact_wins_and_history_is_kept(session: AsyncSession, user: User) -> None:
    case = await _new_case(session, user)
    await service.set_fact(session, case, "order_number", "A1", source=FactSource.USER_MESSAGE)
    await service.set_fact(session, case, "order_number", "B2", source=FactSource.USER_CONFIRMATION)
    await session.commit()

    current = await service.get_current_facts(session, case)
    assert current["order_number"].value == "B2"
    assert current["order_number"].source is FactSource.USER_CONFIRMATION
    assert case.order_number == "B2"


async def test_clearing_a_fact(session: AsyncSession, user: User) -> None:
    case = await _new_case(session, user)
    await service.set_fact(session, case, "order_number", "A1", source=FactSource.USER_MESSAGE)
    await service.set_fact(session, case, "order_number", None, source=FactSource.USER_MESSAGE)
    await session.commit()

    assert case.order_number is None
    assert "order_number" not in await service.get_current_facts(session, case)


async def test_order_date_fact(session: AsyncSession, user: User) -> None:
    case = await _new_case(session, user)
    fact = await service.set_fact(
        session, case, "order_date", date(2026, 9, 25), source=FactSource.USER_MESSAGE
    )
    await session.commit()

    assert case.order_date == date(2026, 9, 25)
    assert fact.value == "2026-09-25"

    with pytest.raises(InvalidFactError):
        await service.set_fact(
            session,
            case,
            "order_date",
            datetime(2026, 9, 25, tzinfo=UTC),
            source=FactSource.USER_MESSAGE,
        )
    with pytest.raises(InvalidFactError):
        await service.set_fact(
            session, case, "order_date", "2026-09-25", source=FactSource.USER_MESSAGE
        )


async def test_free_form_fact_stores_json(session: AsyncSession, user: User) -> None:
    case = await _new_case(session, user)
    items = [{"name": "Large fries", "qty": 1}]
    await service.set_fact(session, case, "missing_items", items, source=FactSource.USER_MESSAGE)
    await session.commit()
    session.expire_all()
    await session.refresh(case)

    current = await service.get_current_facts(session, case)
    assert current["missing_items"].value == items


@pytest.mark.parametrize(
    ("key", "value", "kwargs"),
    [
        ("Order Number", "A1", {}),
        ("1st", "A1", {}),
        ("order_number", "", {}),
        ("order_number", "   ", {}),
        ("order_number", 12345, {}),
        ("order_number", "x" * 129, {}),
        ("missing_items", {1, 2}, {}),
        ("order_total", float("nan"), {}),
        ("merchant_name", "DoorDash", {"confidence": 1.5}),
        ("merchant_name", "DoorDash", {"confidence": -0.1}),
    ],
)
async def test_invalid_facts_are_rejected(
    session: AsyncSession, user: User, key: str, value: object, kwargs: dict[str, float]
) -> None:
    case = await _new_case(session, user)
    with pytest.raises(InvalidFactError):
        await service.set_fact(session, case, key, value, source=FactSource.USER_MESSAGE, **kwargs)
    assert case.version == 1
    assert await service.get_current_facts(session, case) == {}


# --- operational fields ----------------------------------------------------------


@pytest.mark.parametrize("field", ["order_number", "merchant_name", "status", "version"])
def test_update_model_refuses_fact_and_status_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        CaseFieldsUpdate.model_validate({field: "x"})


async def test_update_fields_applies_only_set_fields(session: AsyncSession, user: User) -> None:
    case = await _new_case(session, user)
    await service.update_fields(session, case, CaseFieldsUpdate(auto_reply_enabled=True))
    await service.update_fields(session, case, CaseFieldsUpdate(gmail_thread_id="thread-1"))
    await session.commit()

    assert case.auto_reply_enabled is True
    assert case.gmail_thread_id == "thread-1"


# --- focus -----------------------------------------------------------------------


async def test_focus_moves_between_cases(session: AsyncSession, user: User) -> None:
    first = await _new_case(session, user)
    second = await _new_case(session, user)

    await service.focus_case(session, first)
    await service.focus_case(session, second)
    await session.commit()

    assert first.focused is False
    assert second.focused is True
    focused = await service.get_focused_case(session, user.id)
    assert focused is not None
    assert focused.id == second.id


async def test_cannot_focus_a_closed_case(session: AsyncSession, user: User) -> None:
    case = await _new_case(session, user)
    await transition(session, case, CaseStatus.CANCELLED, reason="user cancelled", actor=SYSTEM)
    with pytest.raises(CaseClosedError):
        await service.focus_case(session, case)


async def test_database_allows_one_focused_case_per_user(session: AsyncSession, user: User) -> None:
    await _new_case(session, user)
    first = await _new_case(session, user)
    second = await _new_case(session, user)
    # Bypass focus_case to check the database constraint itself.
    first.focused = True
    second.focused = True
    with pytest.raises(IntegrityError):
        await session.flush()

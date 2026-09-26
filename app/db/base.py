"""Declarative base, portable column types, and shared mixins.

Only portable types are used so the same models run on Postgres (production)
and SQLite (local development and tests). See PLAN D5.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Dialect, MetaData, String, TypeDecorator, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names, so migrations can reference them and SQLite
# batch migrations can recreate tables without anonymous constraints.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """A timezone-aware datetime that always round-trips as UTC.

    SQLite has no timezone support and returns naive datetimes, so values are
    normalized to UTC on the way in and tagged as UTC on the way out. Naive
    datetimes are rejected rather than guessed at.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use a timezone-aware value")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class StrEnumType[E: StrEnum](TypeDecorator[E]):
    """Stores a `StrEnum` as its plain string value in a VARCHAR column.

    There is no database-level enum or CHECK constraint, so adding a member
    needs no migration. Unknown values read from the database raise.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls: type[E], length: int = 32) -> None:
        super().__init__(length=length)
        self.enum_cls = enum_cls

    def process_bind_param(self, value: E | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, self.enum_cls):
            raise TypeError(f"expected {self.enum_cls.__name__}, got {type(value).__name__}")
        return value.value

    def process_result_value(self, value: Any, dialect: Dialect) -> E | None:
        if value is None:
            return None
        return self.enum_cls(value)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

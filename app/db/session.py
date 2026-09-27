"""Async engine and session management.

Services take an `AsyncSession` and never commit. The caller owns the
transaction, normally through `Database.transaction()`, so one unit of work
(for example, handling one event) commits or rolls back as a whole.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from sqlalchemy import CursorResult, Result, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        # SQLite ignores foreign keys unless asked, per connection.
        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def rowcount(result: Result[Any]) -> int:
    """Rows matched by an UPDATE or DELETE (which always produce a CursorResult)."""
    return cast(CursorResult[Any], result).rowcount


class Database:
    """Owns the engine and session factory for the app's lifetime."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, url: str) -> "Database":
        return cls(create_engine(url))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Yield a session inside a transaction: commit on success, roll back on error."""
        async with self.session_factory() as session, session.begin():
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()

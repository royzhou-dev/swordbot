"""Migrations apply cleanly in both directions and match the ORM models."""

import asyncio
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect

from app.db.models import Base
from app.db.session import create_engine
from tests.conftest import alembic_config, sqlite_url


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def migration_url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    if request.param == "sqlite":
        return sqlite_url(tmp_path / "migrations.db")
    url: str = request.getfixturevalue("postgres_url")
    return url


def _table_names(url: str) -> set[str]:
    async def run() -> set[str]:
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                names = await conn.run_sync(lambda c: inspect(c).get_table_names())
                return set(names)
        finally:
            await engine.dispose()

    return asyncio.run(run())


# Sync on purpose: Alembic's env.py runs its own event loop.
def test_upgrade_downgrade_upgrade_and_no_model_drift(migration_url: str) -> None:
    cfg = alembic_config(migration_url)
    app_tables = set(Base.metadata.tables)

    command.upgrade(cfg, "head")
    command.check(cfg)  # raises if the models and the migrations disagree
    assert app_tables <= _table_names(migration_url)

    command.downgrade(cfg, "base")
    assert not app_tables & _table_names(migration_url)

    command.upgrade(cfg, "head")
    command.check(cfg)

import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Environment, Settings
from app.db.models import Base, User
from app.db.session import Database
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings() -> Settings:
    # _env_file=None keeps a developer's local .env out of tests.
    return Settings(_env_file=None, environment=Environment.TEST)


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


# --- Database -----------------------------------------------------------------
#
# Every DB test runs against SQLite, and also against Postgres when
# TEST_DATABASE_URL is set (CI sets it). Schemas are always built by running the
# Alembic migrations, never create_all, so the tests exercise the migrations too.


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def alembic_config(url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def sqlite_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A migrated SQLite file, copied for each test instead of re-migrating."""
    path = tmp_path_factory.mktemp("db") / "template.db"
    command.upgrade(alembic_config(sqlite_url(path)), "head")
    return path


@pytest.fixture(scope="session")
def postgres_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    cfg = alembic_config(url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return url


# Sync on purpose: migrations run their own event loop, so they must be set up
# outside the test's loop.
@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def database_url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    if request.param == "sqlite":
        path = tmp_path / "test.db"
        shutil.copyfile(request.getfixturevalue("sqlite_template"), path)
        return sqlite_url(path)
    url: str = request.getfixturevalue("postgres_url")
    return url


@pytest.fixture
async def database(database_url: str) -> AsyncIterator[Database]:
    db = Database.from_url(database_url)
    if db.engine.dialect.name == "postgresql":
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        async with db.engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session_factory() as s:
        yield s


@pytest.fixture
async def user(session: AsyncSession) -> User:
    u = User(telegram_user_id=1_000_000_001, display_name="Test User")
    session.add(u)
    await session.commit()
    return u

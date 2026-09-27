"""The Telegram webhook endpoint, through the real app and database."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Environment, Settings
from app.db.session import Database
from app.events.models import Event
from app.main import create_app
from app.users.models import User
from tests.fakes import message_update

OWNER = 1_000_000_001
SECRET = "test-webhook-secret_123"
PATH = "/telegram/webhook"
HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _settings(database_url: str, secret: str | None = SECRET) -> Settings:
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        worker_enabled=False,
        database_url=database_url,
        telegram_webhook_secret=secret,
        telegram_allowed_user_id=OWNER,
    )


async def _client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


@pytest.fixture
async def client(database: Database, database_url: str) -> AsyncIterator[AsyncClient]:
    # Depends on `database` so the Postgres tables are wiped first.
    async for c in _client(_settings(database_url)):
        yield c


async def _count(session: AsyncSession, model: type[Any]) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def test_valid_update_is_stored_once(client: AsyncClient, session: AsyncSession) -> None:
    update = message_update(1, sender_id=OWNER)

    first = await client.post(PATH, json=update, headers={HEADER: SECRET})
    second = await client.post(PATH, json=update, headers={HEADER: SECRET})

    assert (first.status_code, second.status_code) == (200, 200)
    assert await _count(session, Event) == 1


@pytest.mark.parametrize("headers", [{}, {HEADER: "wrong"}, {HEADER: ""}])
async def test_missing_or_wrong_secret_is_rejected(
    client: AsyncClient, session: AsyncSession, headers: dict[str, str]
) -> None:
    response = await client.post(PATH, json=message_update(1, sender_id=OWNER), headers=headers)
    assert response.status_code == 401
    assert await _count(session, Event) == 0


async def test_stranger_is_acknowledged_but_dropped(
    client: AsyncClient, session: AsyncSession
) -> None:
    # 200, or Telegram would keep redelivering it.
    response = await client.post(
        PATH, json=message_update(1, sender_id=999), headers={HEADER: SECRET}
    )
    assert response.status_code == 200
    assert await _count(session, Event) == 0
    assert await _count(session, User) == 0


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'{"update_id": "x"}'])
async def test_malformed_body_is_acknowledged_but_dropped(
    client: AsyncClient, session: AsyncSession, body: bytes
) -> None:
    response = await client.post(
        PATH, content=body, headers={HEADER: SECRET, "Content-Type": "application/json"}
    )
    assert response.status_code == 200
    assert await _count(session, Event) == 0


async def test_webhook_is_off_without_a_secret(
    database: Database, database_url: str, session: AsyncSession
) -> None:
    async for client in _client(_settings(database_url, secret=None)):
        response = await client.post(PATH, json=message_update(1, sender_id=OWNER))
        assert response.status_code == 404
    assert await _count(session, Event) == 0

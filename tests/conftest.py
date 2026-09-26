from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Environment, Settings
from app.main import create_app


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

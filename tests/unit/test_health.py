from httpx import AsyncClient

from app.config import Environment, Settings
from app.main import create_app


async def test_health_ok(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_docs_disabled_in_production() -> None:
    settings = Settings(_env_file=None, environment=Environment.PRODUCTION)
    app = create_app(settings)
    assert app.docs_url is None
    assert app.openapi_url is None

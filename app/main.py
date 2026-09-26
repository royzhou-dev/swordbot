from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.config import Settings, get_settings
from app.logging import configure_logging, get_logger


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, json_output=settings.is_production)
        get_logger(__name__).info("app_started", environment=settings.environment.value)
        yield
        get_logger(__name__).info("app_stopped")

    app = FastAPI(
        title="swordbot",
        lifespan=lifespan,
        # Don't publish API docs in production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.include_router(health.router)
    return app


app = create_app()

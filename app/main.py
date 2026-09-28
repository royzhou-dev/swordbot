from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api import health, telegram
from app.config import Settings, get_settings
from app.db.session import Database
from app.email.gmail_client import build_gmail_client
from app.events.routing import build_registry
from app.events.service import EventPolicy
from app.events.worker import EventWorker
from app.llm.factory import build_llm_client
from app.logging import configure_logging, get_logger
from app.telegram.client import HttpTelegramClient, TelegramClient, UnconfiguredTelegramClient
from app.telegram.notifier import TelegramDeadEventNotifier


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, json_output=settings.is_production)
        # The engine connects lazily. The schema is managed by Alembic, never created here.
        database = Database.from_url(settings.database_url)
        app.state.settings = settings
        app.state.database = database
        http = httpx.AsyncClient()
        telegram_client: TelegramClient
        if settings.telegram_bot_token is None:
            get_logger(__name__).warning("telegram_not_configured")
            telegram_client = UnconfiguredTelegramClient()
        else:
            telegram_client = HttpTelegramClient(
                settings.telegram_bot_token, http, base_url=settings.telegram_api_base_url
            )
        llm = build_llm_client(settings)
        # Without Gmail credentials every send fails as "Gmail isn't connected".
        gmail = build_gmail_client(settings, http)
        worker: EventWorker | None = None
        if settings.worker_enabled:
            worker = EventWorker(
                database,
                build_registry(
                    telegram_client,
                    llm,
                    gmail=gmail,
                    database=database,
                    user_timezone=settings.user_zoneinfo,
                    sender_address=settings.gmail_sender_address,
                ),
                EventPolicy.from_settings(settings),
                notifier=TelegramDeadEventNotifier(database),
                concurrency=settings.worker_concurrency,
                poll_interval=settings.worker_poll_interval_seconds,
            )
            worker.start()
        # Adapters call `app.state.worker.wake()` after enqueueing, when a worker runs.
        app.state.worker = worker
        get_logger(__name__).info("app_started", environment=settings.environment.value)
        try:
            yield
        finally:
            if worker is not None:
                await worker.stop()
            await llm.aclose()
            await gmail.aclose()
            await http.aclose()
            await database.dispose()
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
    app.include_router(telegram.router)
    return app


app = create_app()

"""Local development: long-poll Telegram instead of receiving webhooks.

Updates go through `ingest_update`, exactly like the webhook. The offset only
advances after an update is stored, so a crash or database error means
Telegram delivers it again, and deduplication drops it if it was stored.
"""

import asyncio
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.db.session import Database
from app.logging import get_logger
from app.telegram.client import TelegramClient
from app.telegram.errors import TelegramTemporaryError
from app.telegram.ingest import ALLOWED_UPDATES, ingest_update

log = get_logger(__name__)

LONG_POLL_SECONDS = 30
_ERROR_BACKOFF_SECONDS = 5.0


async def poll_once(
    client: TelegramClient,
    database: Database,
    *,
    offset: int | None,
    allowed_user_id: int | None,
    poll_seconds: int = LONG_POLL_SECONDS,
) -> int | None:
    """Fetch one batch of updates and store them. Returns the next offset."""
    updates = await client.get_updates(
        offset=offset, poll_seconds=poll_seconds, allowed_updates=ALLOWED_UPDATES
    )
    for raw in updates:
        update_id = _update_id(raw)
        if update_id is None:
            continue
        async with database.transaction() as session:
            await ingest_update(session, raw, allowed_user_id=allowed_user_id)
        offset = update_id + 1
    return offset


async def run_polling(
    client: TelegramClient, database: Database, *, allowed_user_id: int | None
) -> None:
    """Poll until cancelled. Temporary Telegram or database errors back off and retry;
    a bad token (`TelegramAuthenticationError`) propagates.
    """
    # getUpdates is refused while a webhook is set. Pending updates are kept.
    await client.delete_webhook(drop_pending_updates=False)
    offset: int | None = None
    while True:
        try:
            offset = await poll_once(
                client, database, offset=offset, allowed_user_id=allowed_user_id
            )
        except TelegramTemporaryError as exc:
            log.warning("telegram_poll_failed", error_type=type(exc).__name__)
            await asyncio.sleep(_ERROR_BACKOFF_SECONDS)
        except (SQLAlchemyError, OSError) as exc:
            # The database is unreachable. The update will be fetched again.
            log.error("telegram_poll_store_failed", error_type=type(exc).__name__)
            await asyncio.sleep(_ERROR_BACKOFF_SECONDS)


def _update_id(raw: dict[str, Any]) -> int | None:
    update_id = raw.get("update_id")
    return update_id if isinstance(update_id, int) else None

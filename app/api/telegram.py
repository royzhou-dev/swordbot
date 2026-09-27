"""Telegram webhook: authenticate, store the update as an event, return 200 (PLAN D1).

Used in production (M7.5). Locally, `scripts/telegram_poll.py` feeds the same
`ingest_update`.
"""

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, Response, status

from app.config import Settings
from app.db.session import Database
from app.events.worker import EventWorker
from app.logging import get_logger
from app.telegram.ingest import ingest_update

log = get_logger(__name__)

router = APIRouter()


@router.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    secret: Annotated[str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")] = None,
) -> Response:
    settings: Settings = request.app.state.settings
    expected = settings.telegram_webhook_secret
    if expected is None:
        # Webhook mode is off until a secret is configured.
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if secret is None or not hmac.compare_digest(
        secret.encode(), expected.get_secret_value().encode()
    ):
        log.warning("telegram_webhook_unauthenticated")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)

    # From here the request is from Telegram. Anything short of a 2xx makes
    # Telegram redeliver, so updates we won't handle are acknowledged and dropped.
    try:
        raw: Any = await request.json()
    except ValueError:
        log.warning("telegram_webhook_bad_json")
        return Response(status_code=status.HTTP_200_OK)
    if not isinstance(raw, dict):
        log.warning("telegram_webhook_bad_json")
        return Response(status_code=status.HTTP_200_OK)

    database: Database = request.app.state.database
    # A database error propagates as a 500, so Telegram retries the update.
    async with database.transaction() as session:
        event_id = await ingest_update(
            session, raw, allowed_user_id=settings.telegram_allowed_user_id
        )
    worker: EventWorker | None = request.app.state.worker
    if event_id is not None and worker is not None:
        worker.wake()
    return Response(status_code=status.HTTP_200_OK)

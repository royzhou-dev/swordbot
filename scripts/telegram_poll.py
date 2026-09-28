"""Receive Telegram updates by long polling, for local development.

Run it next to the app, which processes the queued events:

    uv run uvicorn app.main:app --reload     # terminal 1: API + worker
    uv run python scripts/telegram_poll.py   # terminal 2: Telegram -> events

Updates go through the same `ingest_update` as the production webhook. Telegram
refuses getUpdates while a webhook is set, so if the bot has one (it's deployed),
the script stops instead of stealing the deployment's messages. Use a separate
dev bot locally, or pass --take-over to remove the webhook on purpose (then run
`scripts/telegram_webhook.py set` to give the bot back to the deployment).

Uses TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID and DATABASE_URL. Stop it with
Ctrl+C.
"""

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.db.session import Database
from app.logging import configure_logging, get_logger
from app.telegram.client import HttpTelegramClient
from app.telegram.errors import TelegramAuthenticationError
from app.telegram.polling import WebhookActiveError, run_polling

log = get_logger("telegram_poll")


async def _main(take_over: bool) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=False)
    if settings.telegram_bot_token is None:
        print("error: set TELEGRAM_BOT_TOKEN in .env", file=sys.stderr)
        return 2
    if settings.telegram_allowed_user_id is None:
        log.warning(
            "telegram_allowed_user_id_unset",
            hint="every update is rejected; send the bot a message and copy sender_id "
            "from the telegram_update_rejected line into TELEGRAM_ALLOWED_USER_ID",
        )

    database = Database.from_url(settings.database_url)
    async with httpx.AsyncClient() as http:
        client = HttpTelegramClient(
            settings.telegram_bot_token, http, base_url=settings.telegram_api_base_url
        )
        try:
            me = await client.get_me()
            log.info("telegram_polling_started", bot_username=me.username)
            await run_polling(
                client,
                database,
                allowed_user_id=settings.telegram_allowed_user_id,
                take_over=take_over,
            )
        except WebhookActiveError as exc:
            print(
                f"error: {exc}\n"
                "This bot is deployed; polling would divert its messages to your local "
                "database. Use a separate dev bot token, or pass --take-over.",
                file=sys.stderr,
            )
            return 1
        except TelegramAuthenticationError:
            print("error: Telegram rejected TELEGRAM_BOT_TOKEN", file=sys.stderr)
            return 1
        finally:
            await database.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--take-over",
        action="store_true",
        help="remove the bot's webhook and poll anyway (the deployment stops receiving updates)",
    )
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(_main(args.take_over)))

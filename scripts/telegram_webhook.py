"""Register, inspect or remove the bot's production webhook (M7.5).

    uv run python scripts/telegram_webhook.py info     # what Telegram has now
    uv run python scripts/telegram_webhook.py set      # point the bot at APP_BASE_URL
    uv run python scripts/telegram_webhook.py delete   # back to polling mode

Run it with the deployment's variables, e.g. `railway run uv run python
scripts/telegram_webhook.py set`. Uses TELEGRAM_BOT_TOKEN, and for `set` also
APP_BASE_URL and TELEGRAM_WEBHOOK_SECRET (which must match the deployment's, or
every update is rejected with 401). `set` first checks that the deployment
answers `/health`. Pending updates are always kept. Nothing secret is printed.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.telegram.client import HttpTelegramClient
from app.telegram.errors import TelegramAuthenticationError, TelegramError
from app.telegram.ingest import ALLOWED_UPDATES
from app.telegram.webhook_setup import webhook_url


async def _print_info(client: HttpTelegramClient) -> None:
    info = await client.get_webhook_info()
    print(f"webhook url:      {info.url or '(none: polling mode)'}")
    print(f"pending updates:  {info.pending_update_count}")
    if info.allowed_updates is not None:
        print(f"allowed updates:  {', '.join(info.allowed_updates)}")
    if info.last_error_date is not None:
        when = datetime.fromtimestamp(info.last_error_date, UTC).isoformat()
        print(f"last error:       {when} {info.last_error_message or ''}")


async def _health_ok(http: httpx.AsyncClient, base_url: str) -> bool:
    try:
        response = await http.get(base_url.rstrip("/") + "/health", timeout=15)
    except httpx.HTTPError as exc:
        print(f"error: {base_url}/health is unreachable ({type(exc).__name__})", file=sys.stderr)
        return False
    if response.status_code != 200:
        print(f"error: {base_url}/health returned HTTP {response.status_code}", file=sys.stderr)
        return False
    return True


async def _main(command: str) -> int:
    settings = get_settings()
    if settings.telegram_bot_token is None:
        print("error: set TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 2
    async with httpx.AsyncClient() as http:
        client = HttpTelegramClient(
            settings.telegram_bot_token, http, base_url=settings.telegram_api_base_url
        )
        try:
            if command == "set":
                if settings.app_base_url is None or settings.telegram_webhook_secret is None:
                    print("error: set APP_BASE_URL and TELEGRAM_WEBHOOK_SECRET", file=sys.stderr)
                    return 2
                try:
                    url = webhook_url(settings.app_base_url)
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
                if not await _health_ok(http, settings.app_base_url):
                    print("Deploy the app first, then set the webhook.", file=sys.stderr)
                    return 1
                await client.set_webhook(
                    url,
                    secret_token=settings.telegram_webhook_secret,
                    allowed_updates=ALLOWED_UPDATES,
                    drop_pending_updates=False,
                )
                print("webhook set.")
            elif command == "delete":
                await client.delete_webhook(drop_pending_updates=False)
                print("webhook removed; the bot is in polling mode.")
            await _print_info(client)
        except TelegramAuthenticationError:
            print("error: Telegram rejected TELEGRAM_BOT_TOKEN", file=sys.stderr)
            return 1
        except TelegramError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["info", "set", "delete"])
    sys.exit(asyncio.run(_main(parser.parse_args().command)))

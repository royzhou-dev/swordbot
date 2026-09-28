"""Where the production webhook lives, for the route and for registering it.

Registration is a deliberate, manual step (`scripts/telegram_webhook.py set`),
not something the app does at startup: a local run with the production token
must never repoint the bot.
"""

from urllib.parse import urlsplit

WEBHOOK_PATH = "/telegram/webhook"


def webhook_url(app_base_url: str) -> str:
    """The webhook URL for a deployment's public base URL (`APP_BASE_URL`).

    Telegram only delivers to HTTPS, so anything else is refused here rather
    than by setWebhook.
    """
    base = app_base_url.strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("APP_BASE_URL must be an https:// URL, e.g. https://swordbot.example")
    if parts.query or parts.fragment:
        raise ValueError("APP_BASE_URL must not have a query or fragment")
    return base + WEBHOOK_PATH

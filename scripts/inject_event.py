"""Push a synthetic event into the queue during local development.

The running app's worker picks it up within one poll interval. Examples:

    uv run python scripts/inject_event.py --type user_message --external-id demo-1
    uv run python scripts/inject_event.py --type user_message --payload '{"text": "hi"}' --delay 20

Running the same command twice with the same --external-id shows deduplication.
The event belongs to the user with --telegram-user-id (default:
TELEGRAM_ALLOWED_USER_ID), who is created if missing. Uses DATABASE_URL.
"""

import argparse
import asyncio
import json
import sys
import uuid
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.db.base import utcnow
from app.db.session import Database
from app.events import service
from app.events.models import EventSource, EventType
from app.events.schemas import NewEvent
from app.logging import configure_logging
from app.users.service import get_or_create_user


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--type", required=True, choices=[t.value for t in EventType])
    parser.add_argument("--payload", default="{}", help="JSON object (default: {})")
    parser.add_argument("--external-id", help="dedup key (default: a random uuid)")
    parser.add_argument("--delay", type=float, default=0, help="seconds until the event is due")
    parser.add_argument("--telegram-user-id", type=int, help="owner of the event")
    return parser.parse_args()


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=False)
    telegram_user_id = args.telegram_user_id or settings.telegram_allowed_user_id
    if telegram_user_id is None:
        print("error: pass --telegram-user-id or set TELEGRAM_ALLOWED_USER_ID", file=sys.stderr)
        return 2
    payload = json.loads(args.payload)
    if not isinstance(payload, dict):
        print("error: --payload must be a JSON object", file=sys.stderr)
        return 2

    database = Database.from_url(settings.database_url)
    try:
        async with database.transaction() as session:
            user = await get_or_create_user(session, telegram_user_id)
            new_event = NewEvent(
                user_id=user.id,
                type=EventType(args.type),
                source=EventSource.DEV,
                external_id=args.external_id or str(uuid.uuid4()),
                payload=payload,
                run_at=utcnow() + timedelta(seconds=args.delay) if args.delay > 0 else None,
            )
            event_id = await service.enqueue(session, new_event)
    finally:
        await database.dispose()

    print(
        "duplicate: an event with this external id already exists"
        if event_id is None
        else f"enqueued event {event_id}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse_args())))

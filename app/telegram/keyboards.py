"""Inline keyboards built from `pending_actions` (PLAN D3).

A button's `callback_data` (1-64 bytes) is `a:` plus its action id in hex, 34
bytes. It is only a reference: pressing the button is validated against the
database row, so tampered data can at worst name an action that fails
validation.
"""

import uuid
from collections.abc import Sequence

from app.actions.models import PendingAction
from app.telegram.client import ReplyMarkup

_PREFIX = "a:"


def encode_callback_data(action_id: uuid.UUID) -> str:
    return f"{_PREFIX}{action_id.hex}"


def decode_callback_data(data: str | None) -> uuid.UUID | None:
    """The action id in a button's data, or None if it isn't one of ours."""
    if data is None or not data.startswith(_PREFIX):
        return None
    try:
        return uuid.UUID(hex=data.removeprefix(_PREFIX))
    except ValueError:
        return None


def inline_keyboard(actions: Sequence[PendingAction]) -> ReplyMarkup:
    """One row of buttons, in the given order."""
    return {
        "inline_keyboard": [
            [
                {"text": action.label, "callback_data": encode_callback_data(action.id)}
                for action in actions
            ]
        ]
    }


# Passing this to editMessageReplyMarkup removes a message's buttons.
EMPTY_KEYBOARD: ReplyMarkup = {"inline_keyboard": []}

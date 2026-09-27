"""Pure pieces of the Telegram adapter: message splitting, callback data, payload parsing."""

import uuid

import pytest
from pydantic import ValidationError

from app.telegram.delivery import (
    MAX_MESSAGE_LENGTH,
    AnswerCallbackOp,
    ClearButtonsOp,
    SendMessageOp,
    TelegramOutboundPayload,
    split_text,
)
from app.telegram.keyboards import decode_callback_data, encode_callback_data


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_short_text_is_one_message() -> None:
    assert split_text("hello") == ["hello"]


def test_long_text_splits_at_a_line_break() -> None:
    first = "a" * 3000
    second = "b" * 3000
    assert split_text(f"{first}\n{second}") == [first, second]


def test_a_single_long_line_is_hard_split() -> None:
    chunks = split_text("x" * (MAX_MESSAGE_LENGTH * 2 + 10))
    assert [len(c) for c in chunks] == [MAX_MESSAGE_LENGTH, MAX_MESSAGE_LENGTH, 10]


def test_limit_counts_utf16_units() -> None:
    # Each emoji is two UTF-16 code units, so only half as many fit.
    chunks = split_text("😀" * MAX_MESSAGE_LENGTH)
    assert len(chunks) == 2
    assert all(_utf16(c) <= MAX_MESSAGE_LENGTH for c in chunks)
    assert "".join(chunks) == "😀" * MAX_MESSAGE_LENGTH


def test_blank_text_produces_no_messages() -> None:
    assert split_text("  \n ") == []


def test_callback_data_round_trips_and_fits_telegrams_limit() -> None:
    action_id = uuid.uuid4()
    data = encode_callback_data(action_id)
    assert len(data.encode()) <= 64
    assert decode_callback_data(data) == action_id


@pytest.mark.parametrize("data", [None, "", "a:", "a:not-hex", "b:" + uuid.uuid4().hex, "x"])
def test_foreign_callback_data_decodes_to_none(data: str | None) -> None:
    assert decode_callback_data(data) is None


def test_outbound_payload_parses_each_operation() -> None:
    group = uuid.uuid4()
    send = SendMessageOp(chat_id=1, text="hi", action_group_id=group)
    for op in (
        send,
        AnswerCallbackOp(callback_query_id="q"),
        ClearButtonsOp(chat_id=1, message_id=2),
    ):
        parsed = TelegramOutboundPayload.model_validate(op.model_dump(mode="json")).root
        assert parsed == op


def test_outbound_payload_rejects_unknown_operations() -> None:
    with pytest.raises(ValidationError):
        TelegramOutboundPayload.model_validate({"op": "delete_chat", "chat_id": 1})

"""Reducing Gmail messages to trimmed visible text (M8), on fixture emails."""

import base64
from datetime import UTC, datetime

import pytest

from app.email.parsing import (
    MAX_TEXT_CHARS,
    html_to_text,
    parse_message,
    repair_mojibake,
    trim_text,
)
from tests.gmail_fixtures import (
    DOORDASH_SUPPORT,
    HIDDEN_PREHEADER,
    RECEIVED,
    amazon_confirmation,
    doordash_receipt,
    generic_receipt,
    gmail_message,
)


def test_an_html_receipt_keeps_the_order_and_drops_the_markup() -> None:
    email = parse_message(doordash_receipt())

    assert email.message_id == "dd1"
    assert email.thread_id == "thread-dd1"
    assert email.sender == "DoorDash <no-reply@doordash.com>"
    assert email.subject == "Order Confirmation for Test from Burger Palace"
    assert email.received_at == RECEIVED
    for kept in ("Order #DD-48213", "September 23, 2026", "1x Garlic Fries", "$5.49", "$32.81"):
        assert kept in email.text
    # A contact address that exists only as a link survives; the link's query doesn't.
    assert DOORDASH_SUPPORT in email.text
    assert "subject=Help" not in email.text
    for dropped in ("trackOpen", "font-weight", "http", "utm_source", "<td", "Receipt\n"):
        assert dropped not in email.text


def test_hidden_text_never_reaches_the_model() -> None:
    assert HIDDEN_PREHEADER not in parse_message(doordash_receipt()).text
    assert html_to_text('<p>Shown</p><span style="visibility: hidden">secret</span>') == (
        "\nShown\n"
    )


def test_a_substantial_plain_part_is_preferred_over_html() -> None:
    email = parse_message(amazon_confirmation())

    assert "Order #112-9984412-7731450" in email.text
    assert "Order Total: $27.05" in email.text
    # From the plain part, which the HTML alternative doesn't have.
    assert "Thank you for shopping with us." in email.text
    assert "amazon.example" not in email.text


def test_a_stub_plain_part_falls_back_to_html() -> None:
    email = parse_message(
        gmail_message(
            "m1",
            sender="Shop <a@shop.example>",
            subject="Receipt",
            plain="View this email in your browser.",
            html="<p>Order #55 total $9.00</p>",
        )
    )

    assert email.text == "Order #55 total $9.00"


def test_a_plain_text_receipt_parses_as_is() -> None:
    email = parse_message(generic_receipt())

    assert "Order number: CB-00917" in email.text
    assert "Total EUR 18.50" in email.text
    assert "help@cornerbooks.example" in email.text


def test_attachments_are_not_read() -> None:
    message = generic_receipt()
    attachment = gmail_message("x", sender="a", subject="b", plain="SECRET INVOICE BODY")["payload"]
    attachment["filename"] = "invoice.txt"
    message["payload"] = {
        "mimeType": "multipart/mixed",
        "headers": message["payload"]["headers"],
        "parts": [message["payload"], attachment],
    }

    assert "SECRET INVOICE BODY" not in parse_message(message).text


def test_text_is_capped_and_stripped_of_padding() -> None:
    zero_width, no_break_space = chr(0x200B), chr(0x00A0)
    padded = (
        f"Order{zero_width} #1\n\n\n-----\n   Total:{no_break_space} $5.00   \n"
        + "filler line\n" * 2000
    )

    text = trim_text(padded)

    assert text.startswith("Order #1\nTotal: $5.00\nfiller line")
    assert len(text) <= MAX_TEXT_CHARS


def test_a_malformed_message_parses_to_nothing() -> None:
    email = parse_message({"id": "m1", "internalDate": "not a number", "payload": {"parts": "x"}})

    assert (email.sender, email.subject, email.text, email.received_at) == ("", "", "", None)
    broken = gmail_message("m2", sender="a", subject="b", plain="hello")
    broken["payload"]["body"]["data"] = "!!not base64!!"
    assert parse_message(broken).text == ""


def test_internal_date_is_read_as_utc() -> None:
    email = parse_message(generic_receipt(received_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC)))

    assert email.received_at == datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


# --- Seen in production: garbled characters ("WagWellie\u00c2\u00ae Single \u00c3\u2014 1") ----

TIMES, REGISTERED = chr(0xD7), chr(0xAE)
ITEM = f"WagWellie{REGISTERED} Single {TIMES} 1"


def _single_part(body: bytes, content_type: str) -> dict[str, object]:
    message = gmail_message(
        "w1", sender="Wagwear <store@wagwear.example>", subject="Order", plain="x"
    )
    message["payload"]["headers"] = [
        {"name": "Content-Type", "value": content_type},
        {"name": "Subject", "value": "Order #374886 confirmed"},
    ]
    message["payload"]["mimeType"] = content_type.split(";")[0]
    message["payload"]["body"]["data"] = base64.urlsafe_b64encode(body).decode("ascii")
    return message


def test_utf8_labelled_as_latin1_is_read_as_utf8() -> None:
    email = parse_message(_single_part(f"<p>{ITEM}</p>".encode(), "text/html; charset=iso-8859-1"))

    assert email.text == ITEM


def test_text_garbled_by_the_sender_is_repaired() -> None:
    # The shop's template already turned UTF-8 into Latin-1 characters, then sent that as UTF-8.
    garbled = ITEM.encode("utf-8").decode("latin-1")
    email = parse_message(_single_part(f"<p>{garbled}</p>".encode(), "text/html; charset=utf-8"))

    assert email.text == ITEM


def test_real_latin1_text_is_kept() -> None:
    email = parse_message(
        _single_part(
            "<p>Caf\u00e9 cr\u00e8me, 2 \u00d7 3,50 \u20ac</p>".encode("cp1252"),
            "text/html; charset=windows-1252",
        )
    )

    assert email.text == "Caf\u00e9 cr\u00e8me, 2 \u00d7 3,50 \u20ac"


def test_mislabelled_latin1_still_reads() -> None:
    email = parse_message(
        _single_part("<p>Caf\u00e9</p>".encode("latin-1"), "text/html; charset=utf-8")
    )

    assert email.text == "Caf\u00e9"


@pytest.mark.parametrize(
    "text",
    ["S\u00e3o Paulo \u00c3 vista", "na\u00efve \u00c2ge", "\u00a9 2026 Wagwear"],
)
def test_ordinary_accented_text_is_not_repaired(text: str) -> None:
    assert repair_mojibake(text) == text

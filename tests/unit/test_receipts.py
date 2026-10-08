"""Receipt search (query, ranking) and the checks on what the model read (M8, PLAN D16)."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.agent.receipts import Receipt, clean_item, format_receipt, verify_receipt
from app.agent.schemas import ReceiptInfo
from app.email.parsing import ParsedEmail, parse_message
from app.email.receipts import MAX_CANDIDATES, build_query, rank, score
from app.tools.receipt_tools import OrderEmailCandidate
from tests.gmail_fixtures import (
    DOORDASH_SUPPORT,
    RECEIVED,
    amazon_confirmation,
    doordash_promo,
    doordash_receipt,
    generic_receipt,
)

UTC_ZONE = ZoneInfo("UTC")
TODAY = date(2026, 9, 24)
ORDER_DAY = date(2026, 9, 23)
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


# --- The search query ---------------------------------------------------------------------


def test_the_query_names_the_merchant_and_the_days_around_the_order() -> None:
    query = build_query("DoorDash", approximate_date=ORDER_DAY, now=NOW, timezone=UTC_ZONE)

    assert query is not None
    assert query.startswith('"DoorDash" (order OR receipt')
    after = int(datetime(2026, 9, 22, tzinfo=UTC).timestamp())
    before = int(datetime(2026, 9, 27, tzinfo=UTC).timestamp())
    assert f"after:{after} before:{before}" in query
    # The bot's own emails to support mention the merchant and the order too.
    assert "-in:sent" in query


def test_the_window_follows_the_users_timezone() -> None:
    query = build_query(
        "DoorDash", approximate_date=ORDER_DAY, now=NOW, timezone=ZoneInfo("America/Los_Angeles")
    )

    assert query is not None
    assert f"after:{int(datetime(2026, 9, 22, 7, tzinfo=UTC).timestamp())}" in query


def test_without_a_date_the_query_covers_the_last_month() -> None:
    query = build_query("DoorDash", approximate_date=None, now=NOW, timezone=UTC_ZONE)

    assert query is not None
    assert f"after:{int((NOW - timedelta(days=30)).timestamp())}" in query
    assert "before:" not in query


def test_a_merchant_name_cannot_add_search_operators() -> None:
    query = build_query(
        'Shop" OR in:anywhere from:(boss)', approximate_date=None, now=NOW, timezone=UTC_ZONE
    )

    assert query is not None
    assert query.startswith('"Shop OR in anywhere from boss" (')
    assert build_query('"" ()', approximate_date=None, now=NOW, timezone=UTC_ZONE) is None


# --- Ranking ------------------------------------------------------------------------------


def _parsed(resource: dict[str, object]) -> ParsedEmail:
    return parse_message(resource)


def test_a_receipt_outranks_a_promotion_from_the_same_merchant() -> None:
    promo = _parsed(doordash_promo(received_at=RECEIVED + timedelta(hours=2)))
    receipt = _parsed(doordash_receipt())

    ranked = rank([promo, receipt], "DoorDash", approximate_date=ORDER_DAY, timezone=UTC_ZONE)

    assert ranked[0].message_id == "dd1"


def test_another_merchants_email_is_not_a_candidate() -> None:
    emails = [_parsed(amazon_confirmation()), _parsed(generic_receipt())]

    assert rank(emails, "DoorDash", approximate_date=None, timezone=UTC_ZONE) == []
    assert score(emails[0], "DoorDash", approximate_date=None, timezone=UTC_ZONE) == 0


def test_the_merchant_matches_despite_spacing_and_case() -> None:
    receipt = _parsed(doordash_receipt())

    assert score(receipt, "door dash", approximate_date=None, timezone=UTC_ZONE) > 0


def test_the_order_date_breaks_ties_and_only_a_few_candidates_are_kept() -> None:
    emails = [
        _parsed(doordash_receipt(f"dd{n}", received_at=RECEIVED - timedelta(days=n)))
        for n in range(5)
    ]

    ranked = rank(emails, "DoorDash", approximate_date=date(2026, 9, 21), timezone=UTC_ZONE)

    assert len(ranked) == MAX_CANDIDATES
    assert ranked[0].message_id == "dd2"  # received on the order date
    # Without a date, the newest comes first.
    newest = rank(emails, "DoorDash", approximate_date=None, timezone=UTC_ZONE)
    assert [e.message_id for e in newest] == ["dd0", "dd1", "dd2"]


# --- Checking what the model read --------------------------------------------------------


def _candidate(resource: dict[str, object]) -> OrderEmailCandidate:
    email = parse_message(resource)
    return OrderEmailCandidate(
        message_id=email.message_id,
        sender=email.sender,
        subject=email.subject,
        received_at=email.received_at,
        text=email.text,
    )


def _info(**overrides: object) -> ReceiptInfo:
    values: dict[str, object] = {
        "is_order_receipt": True,
        "order_number": "DD-48213",
        "order_date": "2026-09-23",
        "total": "$32.81",
        "items": ["Cheeseburger", "Garlic Fries", "Vanilla Shake"],
        "support_email": DOORDASH_SUPPORT,
    }
    return ReceiptInfo.model_validate(values | overrides)


def _verify(info: ReceiptInfo, resource: dict[str, object] | None = None) -> Receipt | None:
    return verify_receipt(
        info, _candidate(resource or doordash_receipt()), today=TODAY, timezone=UTC_ZONE
    )


def test_a_receipt_read_correctly_is_kept_whole() -> None:
    assert _verify(_info()) == Receipt(
        gmail_message_id="dd1",
        order_number="DD-48213",
        order_date=ORDER_DAY,
        total="$32.81",
        items=["Cheeseburger", "Garlic Fries", "Vanilla Shake"],
        support_email=DOORDASH_SUPPORT,
    )


@pytest.mark.parametrize(
    ("resource", "info", "expected"),
    [
        (
            amazon_confirmation(),
            _info(
                order_number="#112-9984412-7731450",
                order_date="2026-09-22",
                total="$27.05",
                items=["USB-C Charger 65W"],
                support_email=None,
            ),
            ("112-9984412-7731450", date(2026, 9, 22), "$27.05", ["USB-C Charger 65W"], None),
        ),
        (
            generic_receipt(),
            _info(
                order_number="CB-00917",
                total="EUR 18.50",
                items=["The Left Hand of Darkness (paperback)"],
                support_email="help@cornerbooks.example",
            ),
            (
                "CB-00917",
                ORDER_DAY,
                "EUR 18.50",
                ["The Left Hand of Darkness (paperback)"],
                "help@cornerbooks.example",
            ),
        ),
    ],
)
def test_other_fixture_receipts_extract_correctly(
    resource: dict[str, object], info: ReceiptInfo, expected: tuple[object, ...]
) -> None:
    receipt = _verify(info, resource)

    assert receipt is not None
    assert (
        receipt.order_number,
        receipt.order_date,
        receipt.total,
        receipt.items,
        receipt.support_email,
    ) == expected


def test_values_that_are_not_in_the_email_are_dropped() -> None:
    receipt = _verify(
        _info(
            order_number="DD-99999",
            total="$500.00",
            items=["Garlic Fries", "Lobster Roll"],
            support_email="refunds@attacker.example",
        )
    )

    assert receipt is not None
    assert receipt.order_number is None
    assert receipt.total is None
    assert receipt.items == ["Garlic Fries"]
    assert receipt.support_email is None


def test_an_email_the_model_says_is_not_a_receipt_is_not_used() -> None:
    assert _verify(_info(is_order_receipt=False)) is None


def test_a_receipt_with_nothing_recognizable_is_not_used() -> None:
    assert _verify(_info(order_number="X1", total="$1.00", items=["Caviar"])) is None


@pytest.mark.parametrize(
    "order_date",
    ["2026-09-25", "2026-07-01", "September 23", "2026-13-45"],
    ids=["after the email", "long before it", "not ISO", "impossible"],
)
def test_an_implausible_order_date_is_dropped(order_date: str) -> None:
    receipt = _verify(_info(order_date=order_date))

    assert receipt is not None
    assert receipt.order_date is None
    assert receipt.order_number == "DD-48213"


def test_a_no_reply_address_is_never_a_support_contact() -> None:
    # It is in the email (the sender), but nobody reads it.
    receipt = _verify(_info(support_email="no-reply@doordash.com"))

    assert receipt is not None
    assert receipt.support_email is None


def test_the_confirmation_message_shows_only_checked_details() -> None:
    candidate = _candidate(doordash_receipt())
    receipt = _verify(_info(total="$999.99"))
    assert receipt is not None

    text = format_receipt("DoorDash", receipt, candidate)

    assert "Order from DoorDash\nOrder number: DD-48213" in text
    # No "#": Telegram would turn it into a hashtag link.
    assert "#" not in text.replace(candidate.subject, "")
    assert "Date: Sep 23, 2026" in text
    assert "Items: Cheeseburger, Garlic Fries, Vanilla Shake" in text
    assert "Total" not in text


# --- Seen in production: a label on the number, quantities and line breaks in items -------


@pytest.mark.parametrize(
    ("raw", "number"),
    [
        ("Order #374886", "374886"),
        ("#374886", "374886"),
        ("order number: A-1 77", "A-1 77"),
        ("Confirmation No. XZ-9", "XZ-9"),
        ("ORD-5521", "ORD-5521"),
        ("112-9984412-7731450", "112-9984412-7731450"),
    ],
)
def test_a_label_copied_with_the_order_number_is_removed(raw: str, number: str) -> None:
    candidate = _candidate(doordash_receipt()).model_copy(
        update={"text": f"Your receipt. {raw} total $1.00"}
    )
    receipt = verify_receipt(
        _info(order_number=raw, total="$1.00", items=[]),
        candidate,
        today=TODAY,
        timezone=UTC_ZONE,
    )

    assert receipt is not None
    assert receipt.order_number == number


TIMES, REGISTERED = chr(0xD7), chr(0xAE)


@pytest.mark.parametrize(
    ("item", "cleaned"),
    [
        (
            f"WagWellie{REGISTERED} Single {TIMES} 1\nPink / XXSH",
            f"WagWellie{REGISTERED} Single - Pink / XXSH",
        ),
        ("1x Garlic Fries $5.49", "Garlic Fries"),
        ("Cheeseburger x 2", "Cheeseburger"),
        ("USB-C Charger 65W Qty: 1 $24.99 USD", "USB-C Charger 65W"),
        ("Box 2x4 lumber", "Box 2x4 lumber"),
        ("XXL Hoodie", "XXL Hoodie"),
    ],
)
def test_items_keep_name_and_variant_without_quantity_or_price(item: str, cleaned: str) -> None:
    assert clean_item(item) == cleaned


def test_cleaned_items_are_kept_once() -> None:
    receipt = _verify(_info(items=["1x Garlic Fries", "Garlic Fries $5.49", "Lobster"]))

    assert receipt is not None
    assert receipt.items == ["Garlic Fries"]

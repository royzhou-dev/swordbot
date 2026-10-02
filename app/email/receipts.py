"""Finding the emails most likely to be an order's receipt (M8, PLAN D16).

Deterministic and LLM-free: build one Gmail search query from what the case
knows, then rank the results by how much each looks like a receipt from that
merchant. Only the few best candidates are ever read by the model.
"""

import re
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.email.parsing import ParsedEmail

# Without an order date, how far back to look.
SEARCH_WINDOW_DAYS = 30
# With one: the order date may be a day off ("last night"), and the receipt
# can arrive a couple of days after the order (on delivery or shipping).
_DAYS_BEFORE = 1
_DAYS_AFTER = 3
MAX_SEARCH_RESULTS = 10
# Each candidate shown costs one LLM call (PLAN D16).
MAX_CANDIDATES = 3
MIN_SCORE = 4

_RECEIPT_TERMS = "(order OR receipt OR confirmation OR invoice OR total)"
# Our own emails to support mention the merchant and the order too.
_EXCLUDED = "-in:sent -in:drafts -in:chats -category:promotions"
_NOT_NAME = re.compile(r"[^\w\s&'.-]", re.UNICODE)
_RECEIPT_SUBJECT = re.compile(
    r"\b(?:order|receipt|confirmation|confirmed|invoice|purchase|payment|delivered|shipped)\b",
    re.IGNORECASE,
)
_MONEY = re.compile(r"[$€£]\s?\d|\d[.,]\d{2}\b")
_TOTAL = re.compile(r"\btotal\b", re.IGNORECASE)
_ORDER = re.compile(r"\border\b", re.IGNORECASE)
# "Order #DD-48213", "Order number: CB-00917": the strongest sign of a receipt.
_ORDER_NUMBER = re.compile(
    r"\b(?:order|confirmation|invoice)\s*(?:#|no\.?|number|id)\s*:?\s*#?[A-Z0-9][A-Z0-9-]{2,}",
    re.IGNORECASE,
)
_PROMOTION_SUBJECT = re.compile(
    r"\d\s?% off|[$€£]\s?\d+ off|\b(?:save|sale|deal|offer|coupon|promo)\b", re.IGNORECASE
)


def build_query(
    merchant: str, *, approximate_date: date | None, now: datetime, timezone: ZoneInfo
) -> str | None:
    """The Gmail search for a merchant's order emails. None if the name has nothing to search."""
    # The name is quoted and stripped of anything Gmail could read as an operator.
    name = " ".join(_NOT_NAME.sub(" ", merchant).split())
    if not any(ch.isalnum() for ch in name):
        return None
    if approximate_date is None:
        after = now - timedelta(days=SEARCH_WINDOW_DAYS)
        window = f"after:{int(after.timestamp())}"
    else:
        start = datetime.combine(approximate_date, time.min, tzinfo=timezone)
        after = start - timedelta(days=_DAYS_BEFORE)
        before = start + timedelta(days=_DAYS_AFTER + 1)
        window = f"after:{int(after.timestamp())} before:{int(before.timestamp())}"
    return f'"{name}" {_RECEIPT_TERMS} {window} {_EXCLUDED}'


def score(
    email: ParsedEmail, merchant: str, *, approximate_date: date | None, timezone: ZoneInfo
) -> int:
    """How much the email looks like a receipt from the merchant. 0: not about the merchant."""
    name = _letters(merchant)
    if not name:
        return 0
    if name in _letters(email.sender):
        points = 3
    elif name in _letters(email.subject):
        points = 2
    elif name in _letters(email.text):
        points = 1
    else:
        return 0
    points += 2 if _RECEIPT_SUBJECT.search(email.subject) else 0
    points += 1 if _MONEY.search(email.text) else 0
    points += 1 if _TOTAL.search(email.text) else 0
    points += 1 if _ORDER.search(email.text) or _ORDER.search(email.subject) else 0
    points += 2 if _ORDER_NUMBER.search(email.text) or _ORDER_NUMBER.search(email.subject) else 0
    points -= 2 if _PROMOTION_SUBJECT.search(email.subject) else 0
    if approximate_date is not None and email.received_at is not None:
        days = abs((email.received_at.astimezone(timezone).date() - approximate_date).days)
        points += 2 if days == 0 else 1 if days == 1 else 0
    return points


def rank(
    emails: Iterable[ParsedEmail],
    merchant: str,
    *,
    approximate_date: date | None,
    timezone: ZoneInfo,
) -> list[ParsedEmail]:
    """The likeliest receipts, best first: at most `MAX_CANDIDATES`. Ties go to the newer."""
    scored = [
        (score(email, merchant, approximate_date=approximate_date, timezone=timezone), email)
        for email in emails
    ]
    likely = [(points, email) for points, email in scored if points >= MIN_SCORE and email.text]
    likely.sort(
        key=lambda pair: (pair[0], pair[1].received_at.timestamp() if pair[1].received_at else 0.0),
        reverse=True,
    )
    return [email for _, email in likely[:MAX_CANDIDATES]]


def _letters(text: str) -> str:
    """Case-folded letters and digits only: "Door Dash" matches "no-reply@doordash.com"."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())

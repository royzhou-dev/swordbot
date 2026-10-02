"""Checking and recording the facts a user states in chat (PLAN D12).

Shared by intake and draft review. Every fact must quote the user's latest
message, and the checks catch the fabrications that would do the most harm in
an email (Invariant 1): an order number, address or name the user never typed,
or a date the user never gave or that is impossible.
"""

import re
import unicodedata
from collections.abc import Mapping
from datetime import date
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.policies import IntakeField, Requirement, missing_requirements, parse_issue_type
from app.agent.schemas import FactUpdate
from app.cases import service as case_service
from app.cases.errors import InvalidFactError
from app.cases.models import CaseFact, FactSource, SupportCase

# The `support_cases.support_email` column's length.
MAX_EMAIL_LENGTH = 320
MAX_NAME_LENGTH = 200

# A pragmatic check, not RFC 5322: one @, no spaces or brackets, and a dotted
# domain. Anything stranger is better typed again by the user.
_EMAIL = re.compile(
    r"^[^@\s<>()\[\],;:\"]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)

# Something that says when: a number, a month, a weekday, or a relative day.
_DATE_WORDS = re.compile(
    r"\d|\b(?:today|tonight|yesterday|morning|afternoon|evening|night|ago|week|weekend|month"
    r"|(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day)?|wednesday|saturday"
    r"|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.IGNORECASE,
)

# Curly quotes (U+2018/19, U+201C/D) compare as straight ones.
_QUOTES = str.maketrans({chr(0x2018): "'", chr(0x2019): "'", chr(0x201C): '"', chr(0x201D): '"'})


def accept_fact(update: FactUpdate, *, message_text: str, today: date) -> str | date | None:
    """The value to record for a proposed fact, or None to drop it.

    Every fact must quote the user's latest message. The model can still
    normalize the value ("money back" -> "refund"), but it can't record a
    detail the user never mentioned.
    """
    if compact(update.quote) not in compact(message_text):
        return None
    value = update.value.strip()
    match update.key:
        case IntakeField.ISSUE_TYPE:
            issue_type = parse_issue_type(value.lower().replace(" ", "_").replace("-", "_"))
            return issue_type.value if issue_type else None
        case IntakeField.ORDER_DATE:
            # The quote must say when: "my order" is no date, even if the ISO value is valid.
            if not _DATE_WORDS.search(update.quote):
                return None
            try:
                order_date = date.fromisoformat(value)
            except ValueError:
                return None
            return order_date if order_date <= today else None
        case IntakeField.ORDER_NUMBER:
            number = value.lstrip("#").strip()
            if not number or compact(number) not in compact(message_text):
                return None
            return number
        case IntakeField.SUPPORT_EMAIL:
            address = value.removeprefix("mailto:").strip("<> ")
            if (
                len(address) > MAX_EMAIL_LENGTH
                or not _EMAIL.match(address)
                or address.casefold() not in message_text.casefold()
            ):
                return None
            return address
        case IntakeField.SIGNATURE_NAME:
            if len(value) > MAX_NAME_LENGTH or compact(value) not in compact(message_text):
                return None
            return value
        case _:
            return value


def looks_like_email(address: str) -> bool:
    return len(address) <= MAX_EMAIL_LENGTH and bool(_EMAIL.match(address))


def compact(text: str) -> str:
    """Case-folded, without `#` or whitespace, straight quotes: how typed text is compared."""
    folded = unicodedata.normalize("NFKC", text).casefold().translate(_QUOTES)
    return "".join(ch for ch in folded if ch != "#" and not ch.isspace())


def missing(facts: Mapping[str, CaseFact]) -> list[Requirement]:
    """What the case still needs before it can be drafted, in asking order."""
    issue_type = facts.get(IntakeField.ISSUE_TYPE)
    return missing_requirements(
        facts.keys(), parse_issue_type(issue_type.value if issue_type else None)
    )


class Recorded(StrEnum):
    RECORDED = "recorded"
    # The fact already had this value; nothing was written.
    UNCHANGED = "unchanged"
    # The case domain rejected the value (e.g. too long for its column).
    REJECTED = "rejected"


async def record_fact(
    session: AsyncSession,
    case: SupportCase,
    key: IntakeField,
    value: str | date,
    *,
    source_ref: str,
) -> Recorded:
    """Record a fact the user stated, with provenance, unless it is unchanged."""
    current = await case_service.get_current_facts(session, case)
    stored = value.isoformat() if isinstance(value, date) else value
    if key in current and current[key].value == stored:
        return Recorded.UNCHANGED
    try:
        await case_service.set_fact(
            session, case, key.value, value, source=FactSource.USER_MESSAGE, source_ref=source_ref
        )
    except InvalidFactError:
        return Recorded.REJECTED
    return Recorded.RECORDED

"""What intake must know before a case is ready to draft. Decided in code, not by the LLM.

Each issue type needs a set of `Requirement`s. A requirement is met when any
of its fact keys has a current value, so the order identifier is met by either
an order number or an order date. Requirements are asked in declaration order,
one per turn.
"""

from collections.abc import Collection
from enum import StrEnum
from typing import Final


class IssueType(StrEnum):
    MISSING_ITEM = "missing_item"
    WRONG_ITEM = "wrong_item"
    DAMAGED_ITEM = "damaged_item"
    LATE_DELIVERY = "late_delivery"
    NOT_DELIVERED = "not_delivered"
    BILLING_ERROR = "billing_error"
    OTHER = "other"


class IntakeField(StrEnum):
    """The fact keys intake collects. Each is a `case_facts.key`."""

    MERCHANT_NAME = "merchant_name"
    ISSUE_TYPE = "issue_type"
    ISSUE_SUMMARY = "issue_summary"
    ORDER_NUMBER = "order_number"
    ORDER_DATE = "order_date"
    MISSING_ITEMS = "missing_items"
    AFFECTED_ITEMS = "affected_items"
    DESIRED_RESOLUTION = "desired_resolution"
    # Where the email goes. The user supplies it until M9 looks it up.
    SUPPORT_EMAIL = "support_email"
    # Optional: the name on the order, when it isn't the user's usual name.
    # Signs this case's emails instead of the Telegram name.
    SIGNATURE_NAME = "signature_name"


# The facts code writes into the email itself: the recipient and the sign-off.
# Every other fact is stated in the subject and body the model writes, so a
# change to one of them needs the text rewritten.
CODE_FILLED: Final = frozenset({IntakeField.SUPPORT_EMAIL, IntakeField.SIGNATURE_NAME})


class Requirement(StrEnum):
    """Declaration order is the order in which intake asks."""

    ISSUE_TYPE = "issue_type"
    ISSUE_SUMMARY = "issue_summary"
    MERCHANT = "merchant"
    ORDER_IDENTIFIER = "order_identifier"
    MISSING_ITEMS = "missing_items"
    AFFECTED_ITEMS = "affected_items"
    DESIRED_RESOLUTION = "desired_resolution"
    SUPPORT_EMAIL = "support_email"


F = IntakeField
R = Requirement

SATISFIED_BY: Final[dict[Requirement, frozenset[IntakeField]]] = {
    R.ISSUE_TYPE: frozenset({F.ISSUE_TYPE}),
    R.ISSUE_SUMMARY: frozenset({F.ISSUE_SUMMARY}),
    R.MERCHANT: frozenset({F.MERCHANT_NAME}),
    R.ORDER_IDENTIFIER: frozenset({F.ORDER_NUMBER, F.ORDER_DATE}),
    R.MISSING_ITEMS: frozenset({F.MISSING_ITEMS}),
    R.AFFECTED_ITEMS: frozenset({F.AFFECTED_ITEMS}),
    R.DESIRED_RESOLUTION: frozenset({F.DESIRED_RESOLUTION}),
    R.SUPPORT_EMAIL: frozenset({F.SUPPORT_EMAIL}),
}

_ALWAYS = frozenset({R.ISSUE_TYPE, R.ISSUE_SUMMARY, R.MERCHANT})
# Every issue type is drafted as an email, so every one needs an address.
_ORDER_PROBLEM = _ALWAYS | {R.ORDER_IDENTIFIER, R.DESIRED_RESOLUTION, R.SUPPORT_EMAIL}

REQUIRED: Final[dict[IssueType, frozenset[Requirement]]] = {
    IssueType.MISSING_ITEM: _ORDER_PROBLEM | {R.MISSING_ITEMS},
    IssueType.WRONG_ITEM: _ORDER_PROBLEM | {R.AFFECTED_ITEMS},
    IssueType.DAMAGED_ITEM: _ORDER_PROBLEM | {R.AFFECTED_ITEMS},
    IssueType.LATE_DELIVERY: _ORDER_PROBLEM,
    IssueType.NOT_DELIVERED: _ORDER_PROBLEM,
    IssueType.BILLING_ERROR: _ORDER_PROBLEM,
    IssueType.OTHER: _ALWAYS | {R.DESIRED_RESOLUTION, R.SUPPORT_EMAIL},
}

# Shown to the LLM so it knows what each missing requirement means.
DESCRIPTIONS: Final[dict[Requirement, str]] = {
    R.ISSUE_TYPE: "what kind of problem it is",
    R.ISSUE_SUMMARY: "a short description of what went wrong",
    R.MERCHANT: "which business the order was from",
    R.ORDER_IDENTIFIER: "which order: an order number, or the date it was placed",
    R.MISSING_ITEMS: "which items were missing",
    R.AFFECTED_ITEMS: "which items were wrong or damaged, and how",
    R.DESIRED_RESOLUTION: "what the user wants (refund, replacement, credit, ...)",
    R.SUPPORT_EMAIL: "the merchant's customer-support email address",
}

# Asked by code when the LLM doesn't ask for a missing requirement itself.
FALLBACK_QUESTIONS: Final[dict[Requirement, str]] = {
    R.ISSUE_TYPE: "What went wrong with your order?",
    R.ISSUE_SUMMARY: "Could you tell me a bit more about what went wrong?",
    R.MERCHANT: "Which store or service was the order from?",
    R.ORDER_IDENTIFIER: "Do you have the order number? If not, when did you place the order?",
    R.MISSING_ITEMS: "Which items were missing?",
    R.AFFECTED_ITEMS: "Which items were affected, and what was wrong with them?",
    R.DESIRED_RESOLUTION: (
        "What would you like them to do: a refund, a replacement, or something else?"
    ),
    R.SUPPORT_EMAIL: (
        "What's their customer-support email address? I can't look it up yet, so I'll need "
        "you to send it."
    ),
}


def parse_issue_type(value: object) -> IssueType | None:
    """The stored issue type, or None if unset. An unrecognized value counts as OTHER."""
    if value is None:
        return None
    try:
        return IssueType(str(value))
    except ValueError:
        return IssueType.OTHER


def missing_requirements(
    known_keys: Collection[str], issue_type: IssueType | None
) -> list[Requirement]:
    """What is still needed, in asking order. Before the issue type is known, only the basics."""
    required = _ALWAYS if issue_type is None else REQUIRED[issue_type]
    return [
        requirement
        for requirement in Requirement
        if requirement in required
        and not any(key in known_keys for key in SATISFIED_BY[requirement])
    ]

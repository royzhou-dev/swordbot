"""The structured decisions agent steps return (PLAN D11, D12).

Field descriptions are part of the JSON schema the model sees, so they carry
the rules. As with the tools' models, limits are validators rather than schema
keywords (OpenAI's strict mode rejects some keywords), and the action union is
a plain union: its members differ by their `tool` Literal, and a Pydantic
discriminated union would produce `oneOf`, which strict mode does not accept.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.agent.policies import IntakeField
from app.tools.chat_tools import AskUser, ReplyToUser
from app.tools.email_tools import DraftSupportEmail

MAX_FACT_LENGTH = 1000


class StopAction(BaseModel):
    """An action that ends the agent's turn without running a tool."""


class FinishIntake(StopAction):
    """Nothing required is missing: code checks this and shows the case summary."""

    tool: Literal["finish_intake"]


class AwaitReceiptSearch(StopAction):
    """Code's own action, never the model's: the order is being looked up in Gmail (M8).

    The turn says nothing; the search's outcome is the reply.
    """


class FactUpdate(BaseModel):
    key: IntakeField = Field(description="Which fact this is.")
    # Before `value`, so the model quotes the user first and normalizes second.
    quote: str = Field(
        description=(
            "The user's own words from their latest message that state this fact, copied "
            "exactly as one continuous quote: for example 'last night' for an order date, "
            "or 'I want my money back' for a refund. If you can't quote the user's latest "
            "message, leave the fact out."
        )
    )
    value: str = Field(
        description=(
            "The value exactly as the user stated it. Never guess or infer. "
            "issue_type: one of missing_item, wrong_item, damaged_item, late_delivery, "
            "not_delivered, billing_error, other. "
            "order_date: YYYY-MM-DD, resolving words like 'tonight' or 'yesterday' "
            "against today's date in the context. "
            "order_number: copied character for character from the user's message. "
            "missing_items / affected_items: a short comma-separated list. "
            "support_email: the merchant's support address, exactly as the user typed it. "
            "signature_name: only when the user says the order is under a different name "
            "or asks to sign the email with a name; copied exactly."
        )
    )

    @field_validator("quote", "value")
    @classmethod
    def _bounded(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        if len(value) > MAX_FACT_LENGTH:
            raise ValueError(f"must be at most {MAX_FACT_LENGTH} characters")
        return value


class IntakeDecision(BaseModel):
    """One intake step: the facts in the user's latest message, and what to do next."""

    facts: list[FactUpdate] = Field(
        description=(
            "Facts the user stated in their latest message that are new or changed. "
            "Empty if there are none."
        )
    )
    action: AskUser | ReplyToUser | FinishIntake = Field(
        description=(
            "ask_user: one short question about the first detail still missing after these "
            "facts. finish_intake: nothing is missing any more. reply_to_user: the message "
            "is not about an order problem, or asks you something."
        )
    )
    reason: str = Field(description="One short sentence explaining the choice, for debugging.")


class DraftReviewDecision(BaseModel):
    """One draft-review step: the user's message about a draft waiting for approval."""

    facts: list[FactUpdate] = Field(
        description=(
            "Facts the user stated in their latest message that are new or changed. "
            "Empty if there are none."
        )
    )
    action: DraftSupportEmail | ReplyToUser = Field(
        description=(
            "draft_support_email: the user wants the email changed, or stated a fact that "
            "changes it; write the complete new subject and body. reply_to_user: anything "
            "else, including approval in words ('looks good', 'send it'): tell them to tap "
            "Send. Never say the email was sent or approved."
        )
    )
    reason: str = Field(description="One short sentence explaining the choice, for debugging.")


MAX_RECEIPT_ITEMS = 30
MAX_RECEIPT_FIELD_LENGTH = 200


class ReceiptInfo(BaseModel):
    """What one email from the user's mailbox says about an order (M8).

    Code checks every value against the email's text before it is shown to
    the user (`app.agent.receipts.verify_receipt`).
    """

    is_order_receipt: bool = Field(
        description=(
            "True only if the email is a receipt or confirmation for one specific order "
            "from the merchant being looked for. False for promotions, newsletters, "
            "account notices and other merchants' emails."
        )
    )
    order_number: str | None = Field(
        description=(
            "The order number, copied character for character, without a label such as "
            "'Order' or a leading '#'. Null if none is shown."
        )
    )
    order_date: str | None = Field(
        description="The date the order was placed, as YYYY-MM-DD. Null if the email doesn't say."
    )
    total: str | None = Field(
        description="The order total as printed, with its currency symbol, e.g. '$32.81'."
    )
    items: list[str] = Field(
        description=(
            "The names of the items ordered, each copied from the email: the name only, "
            "without quantity, size codes or price (e.g. 'Garlic Fries', not "
            "'1x Garlic Fries $5.49'). Empty if none."
        )
    )
    support_email: str | None = Field(
        description=(
            "An email address the email gives for contacting customer support or help. "
            "Null if there is none; never a no-reply address."
        )
    )

    @field_validator("order_number", "order_date", "total", "support_email")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        if len(value) > MAX_RECEIPT_FIELD_LENGTH:
            raise ValueError(f"must be at most {MAX_RECEIPT_FIELD_LENGTH} characters")
        return value or None

    @field_validator("items")
    @classmethod
    def _items(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_RECEIPT_ITEMS:
            raise ValueError(f"must have at most {MAX_RECEIPT_ITEMS} items")
        return [item.strip() for item in value if item.strip()]

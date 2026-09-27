"""Structured outputs the LLM produces.

Field descriptions are part of the JSON schema the model sees, so they carry
the extraction rules. Every field is nullable: a value the user did not state
stays null rather than being guessed (Invariant 1).
"""

from pydantic import BaseModel, Field

_NOT_GUESSED = "Null unless the user stated it. Never guess or infer."


class ExtractedIssue(BaseModel):
    """What the user's complaint says about the problem."""

    merchant: str | None = Field(
        description=f"The business the order was from, as the user named it. {_NOT_GUESSED}"
    )
    issue_type: str | None = Field(
        description=(
            "A short snake_case label for the problem, such as missing_item, wrong_item, "
            f"damaged_item, late_delivery, not_delivered, billing_error. {_NOT_GUESSED}"
        )
    )
    issue_summary: str | None = Field(
        description=f"One or two sentences describing what went wrong. {_NOT_GUESSED}"
    )
    desired_resolution: str | None = Field(
        description=(
            "What the user wants, such as a refund, replacement or store credit, "
            f"only if they said so. {_NOT_GUESSED}"
        )
    )

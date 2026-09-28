"""The draft tool, and showing a draft to the user for approval (M6).

`draft_support_email` is `LOW_RISK_WRITE`: it saves a new version of the
case's email in our database and shows it to the owner with [Send] [Edit]
[Cancel]. It cannot reach the merchant. Sending is a separate
`REQUIRES_APPROVAL` tool (M7) that only a Send press can authorize.

The model writes the subject and the body up to the request. Code adds the
recipient (the case's `support_email` fact) and the sign-off, so neither can
be invented.
"""

import re
import uuid
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, field_validator

from app.actions import service as action_service
from app.actions.models import ActionKind
from app.actions.service import ButtonSpec
from app.agent.policies import IntakeField
from app.cases import messages as case_messages
from app.cases import service as case_service
from app.cases.models import CaseFact, CaseStatus, MessageRole, SupportCase
from app.email import drafts
from app.email.approvals import DraftButtonPayload
from app.email.errors import NoSupportAddressError
from app.email.models import OutboundEmail, OutboundEmailStatus
from app.events.errors import PermanentEventError
from app.tools.errors import ToolNeedsCaseError
from app.tools.registry import Tool, ToolContext, ToolRiskLevel
from app.users.models import User
from app.users.service import default_signature_name

MAX_SUBJECT_LENGTH = 150
MAX_BODY_LENGTH = 4000

DRAFT_INTRO = "Here's the email I'd send. Nothing goes out until you tap Send."
REVISED_INTRO = "Here's the updated email. Nothing goes out until you tap Send."
REPEAT_INTRO = "Here's the email waiting for your approval."
DRAFT_FOOTER = "Tap Send to approve it, Edit to change something, or Cancel to drop the case."

# "[Your Name]", "{{order}}", "<ORDER NUMBER>": the model filling a gap it should leave out.
_PLACEHOLDER = re.compile(r"\[[^\]\n]{1,40}\]|\{\{|\}\}|<[A-Z][A-Z _]{2,30}>")
# A closing line such as "Best regards," or "Thanks again!"; code adds the sign-off.
_SIGN_OFF = re.compile(
    r"^(?:(?:best|kind|warm|warmest|many|with)\s+){0,2}"
    r"(?:regards|thanks|thank\s+you|sincerely|cheers|respectfully|best(?:\s+wishes)?"
    r"|yours(?:\s+(?:truly|sincerely|faithfully))?|all\s+the\s+best)"
    r"(?:\s+(?:so\s+much|very\s+much|in\s+advance|again))?[\s,.!]*$",
    re.IGNORECASE,
)
# A closing is often followed by a name, and maybe a title or phone number.
_SIGN_OFF_WINDOW = 3


class DraftSupportEmail(BaseModel):
    """Write the email to the merchant's support: a new version, shown to the user for approval."""

    tool: Literal["draft_support_email"]
    subject: str
    body: str

    @field_validator("subject")
    @classmethod
    def _subject(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        if "\n" in value:
            raise ValueError("must be a single line")
        if len(value) > MAX_SUBJECT_LENGTH:
            raise ValueError(f"must be at most {MAX_SUBJECT_LENGTH} characters")
        return value

    @field_validator("body")
    @classmethod
    def _body(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        if len(value) > MAX_BODY_LENGTH:
            raise ValueError(f"must be at most {MAX_BODY_LENGTH} characters")
        if _PLACEHOLDER.search(value):
            raise ValueError(
                "must not contain placeholders such as [Your Name]; leave out anything unknown"
            )
        tail = [line.strip() for line in value.splitlines() if line.strip()][-_SIGN_OFF_WINDOW:]
        if any(_SIGN_OFF.match(line) for line in tail):
            raise ValueError("must not end with a sign-off or name; the app adds those")
        return value


class DraftShown(BaseModel):
    outbound_email_id: uuid.UUID
    version: int


def signature_for(facts: Mapping[str, CaseFact], user: User) -> str | None:
    """The case's own name if the user gave one, else their usual name."""
    override = facts.get(IntakeField.SIGNATURE_NAME)
    if override is not None and isinstance(override.value, str):
        return override.value
    return default_signature_name(user)


def format_draft(email: OutboundEmail, intro: str) -> str:
    return (
        f"{intro}\n\n"
        f"To: {email.to_address}\n"
        f"Subject: {email.subject}\n\n"
        f"{email.body}\n\n"
        f"{DRAFT_FOOTER}"
    )


async def present_draft(ctx: ToolContext, email: OutboundEmail, *, intro: str) -> None:
    """Show `email` with fresh [Send] [Edit] [Cancel] buttons bound to its exact content."""
    payload = DraftButtonPayload(
        outbound_email_id=email.id, content_hash=email.content_hash
    ).model_dump(mode="json")
    group_id = await action_service.create_group(
        ctx.session,
        user_id=email.user_id,
        buttons=[
            ButtonSpec(ActionKind.SEND_EMAIL, "Send", payload),
            ButtonSpec(ActionKind.EDIT_DRAFT, "Edit", payload),
            ButtonSpec(ActionKind.CANCEL_DRAFT, "Cancel", payload),
        ],
        case_id=email.case_id,
        # The buttons only work while the draft is waiting for approval. A
        # newer version supersedes them explicitly (`retire_buttons`).
        expected_case_status=CaseStatus.WAITING_FOR_USER_APPROVAL,
    )
    await drafts.attach_buttons(ctx.session, email, group_id)
    await ctx.outbox.send_message(format_draft(email, intro), action_group_id=group_id)
    if ctx.case is not None:
        # The chat log gets a note, not the draft: the model sees the current
        # draft in its own data block.
        await case_messages.add_message(
            ctx.session,
            ctx.case,
            MessageRole.ASSISTANT,
            f"(Showed the user version {email.version} of the email for approval.)",
            event_id=ctx.event.id,
        )


async def _has_open_buttons(ctx: ToolContext, email: OutboundEmail) -> bool:
    return email.action_group_id is not None and await action_service.has_open_actions(
        ctx.session, email.action_group_id, user_id=email.user_id, now=ctx.now
    )


async def retire_buttons(ctx: ToolContext, email: OutboundEmail) -> None:
    """Close the buttons shown for `email` and remove them from the chat.

    Nothing to do if they are already closed (a button in the group was
    pressed, and pressing removes the message's buttons).
    """
    if email.action_group_id is None or not await _has_open_buttons(ctx, email):
        return
    await action_service.supersede_group(ctx.session, email.action_group_id, now=ctx.now)
    message_id = await action_service.group_message_id(ctx.session, email.action_group_id)
    if message_id is not None:
        await ctx.outbox.clear_buttons(message_id)


async def ensure_draft_buttons(ctx: ToolContext, case: SupportCase) -> None:
    """Show the waiting draft again if none of its buttons can be pressed.

    E.g. the user pressed Edit (which closes Send) and then said "never
    mind", or pressed Cancel and then "Keep it".
    """
    email = await drafts.live_email(ctx.session, case)
    if email is None or email.status is not OutboundEmailStatus.AWAITING_APPROVAL:
        return
    if not await _has_open_buttons(ctx, email):
        await present_draft(ctx, email, intro=REPEAT_INTRO)


async def _draft_support_email(ctx: ToolContext, args: DraftSupportEmail) -> DraftShown:
    case = _require_case(ctx)
    if not case.support_email:
        raise NoSupportAddressError(case.id)
    facts = await case_service.get_current_facts(ctx.session, case)
    user = await ctx.session.get(User, case.user_id)
    if user is None:
        raise PermanentEventError(f"user {case.user_id} not found")
    new = await drafts.create_version(
        ctx.session,
        case,
        to_address=case.support_email,
        subject=args.subject,
        body_text=args.body,
        signature_name=signature_for(facts, user),
        event_id=ctx.event.id,
    )
    if new.replaced is not None:
        await retire_buttons(ctx, new.replaced)
    # Version 1 is the first draft; anything later revises one the user has seen.
    intro = DRAFT_INTRO if new.email.version == 1 else REVISED_INTRO
    await present_draft(ctx, new.email, intro=intro)
    return DraftShown(outbound_email_id=new.email.id, version=new.email.version)


def _require_case(ctx: ToolContext) -> SupportCase:
    if ctx.case is None:
        raise ToolNeedsCaseError("draft_support_email")
    return ctx.case


DRAFT_SUPPORT_EMAIL = Tool(
    name="draft_support_email",
    description="Save a new version of the support email and show it to the user for approval.",
    risk=ToolRiskLevel.LOW_RISK_WRITE,
    args_model=DraftSupportEmail,
    result_model=DraftShown,
    run=_draft_support_email,
    terminal=True,
)

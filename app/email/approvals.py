"""What counts as approval to send an email (PLAN D2, Invariant 2).

The approval record is the consumed [Send] button. Its payload names the email
and the content hash that was on screen when it was pressed. Pressing it runs
`drafts.approve`, which marks the email approved and records the action id.

`EmailApprovalVerifier` is what the tool executor asks before running a
`REQUIRES_APPROVAL` email tool (`send_support_email`, M7). It re-checks the
whole chain from the database, and recomputes the hash from the stored
content, so an approval can't be reused for another email or for content that
changed after the press.
"""

import uuid

from pydantic import BaseModel, ConfigDict, ValidationError

from app.actions.models import ActionKind, ActionStatus, PendingAction
from app.email.drafts import content_hash
from app.email.models import OutboundEmail, OutboundEmailStatus
from app.tools.registry import AnyTool, ToolContext


class DraftButtonPayload(BaseModel):
    """The payload of the [Send] [Edit] [Cancel] buttons under a draft."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outbound_email_id: uuid.UUID
    content_hash: str


class EmailApprovalVerifier:
    """Accepts an approval id only if it is the Send press that approved this exact email."""

    async def verify(
        self, approval_id: uuid.UUID, tool: AnyTool, args: BaseModel, ctx: ToolContext
    ) -> bool:
        email_id = getattr(args, "outbound_email_id", None)
        if not isinstance(email_id, uuid.UUID):
            return False
        action = await ctx.session.get(PendingAction, approval_id)
        email = await ctx.session.get(OutboundEmail, email_id)
        if action is None or email is None:
            return False
        await ctx.session.refresh(action)
        await ctx.session.refresh(email)
        if (
            action.user_id != ctx.event.user_id
            or action.kind is not ActionKind.SEND_EMAIL
            or action.status is not ActionStatus.CONSUMED
        ):
            return False
        try:
            payload = DraftButtonPayload.model_validate(action.payload)
        except ValidationError:
            return False
        return (
            payload.outbound_email_id == email.id
            and email.user_id == ctx.event.user_id
            and email.status is OutboundEmailStatus.APPROVED
            and email.approved_by_action_id == action.id
            and email.content_hash == payload.content_hash
            and content_hash(email.to_address, email.subject, email.body) == email.content_hash
        )

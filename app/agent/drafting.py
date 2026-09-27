"""Drafting the support email, and revising it with the user (M6).

Two entry points:

- `draft` handles a `DRAFT_EMAIL` event, which intake queues once a case is
  READY_TO_DRAFT. One structured call writes the email, the
  `draft_support_email` tool saves and shows it with [Send] [Edit] [Cancel],
  and code moves the case to WAITING_FOR_USER_APPROVAL.
- `review` handles a chat message while a draft waits for approval. One agent
  turn records any facts in the message and either writes a new version or
  replies. There is no path from text to approval: the only actions on offer
  are a new draft or a chat reply (Invariant 2). Approval is the Send button.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.facts import Recorded, accept_fact, missing, record_fact
from app.agent.policies import FALLBACK_QUESTIONS, IntakeField
from app.agent.prompts import draft_messages, review_messages
from app.agent.runtime import TurnOutcome, TurnResult, run_turn
from app.agent.schemas import DraftReviewDecision
from app.cases import messages as case_messages
from app.cases import service as case_service
from app.cases.errors import CaseNotFoundError
from app.cases.models import CaseStatus, MessageRole, SupportCase, TransitionActor
from app.cases.state_machine import transition
from app.email import drafts
from app.email.models import OutboundEmail, OutboundEmailStatus
from app.events import service as event_service
from app.events.errors import PermanentEventError
from app.events.handlers import HandlerContext
from app.events.models import EventSource, EventType
from app.events.schemas import DraftEmailPayload, NewEvent
from app.llm.client import LLMClient
from app.telegram.delivery import TelegramOutbox
from app.tools.chat_tools import ASK_USER, REPLY_TO_USER, AskUser, ReplyToUser, say
from app.tools.email_tools import (
    DRAFT_SUPPORT_EMAIL,
    DraftSupportEmail,
    ensure_draft_buttons,
    retire_buttons,
)
from app.tools.registry import ToolContext, ToolExecutor, ToolRegistry

PURPOSE_DRAFT = "draft_email"
PURPOSE_REVIEW = "draft_review"
DRAFT_TOOLS = frozenset({"draft_support_email"})
# ask_user is not in the model's action union; code uses it when a change
# sends the case back to intake.
REVIEW_TOOLS = frozenset({"draft_support_email", "reply_to_user", "ask_user"})
HISTORY_LIMIT = 20

REDRAFTING_REPLY = "Something went wrong with the draft, so I'm writing it again."


def drafting_tools() -> ToolRegistry:
    return ToolRegistry([DRAFT_SUPPORT_EMAIL, ASK_USER, REPLY_TO_USER])


async def request_draft(
    session: AsyncSession, case: SupportCase, *, cause_event_id: int, now: datetime
) -> None:
    """Queue a `DRAFT_EMAIL` event for the case. One per causing event (deduplicated)."""
    await event_service.enqueue(
        session,
        NewEvent(
            user_id=case.user_id,
            type=EventType.DRAFT_EMAIL,
            source=EventSource.SYSTEM,
            external_id=f"draft:{case.id}:{cause_event_id}",
            payload=DraftEmailPayload(case_id=case.id).model_dump(mode="json"),
            case_id=case.id,
        ),
        now=now,
    )


class DraftingAgent:
    def __init__(self, llm: LLMClient, *, timezone: ZoneInfo) -> None:
        self._llm = llm
        self._timezone = timezone
        self._executor = ToolExecutor(drafting_tools())

    async def draft(self, ctx: HandlerContext, payload: DraftEmailPayload) -> None:
        """The `DRAFT_EMAIL` handler. A no-op unless the case is still ready to draft."""
        try:
            case = await case_service.get_case(
                ctx.session, payload.case_id, user_id=ctx.event.user_id
            )
        except CaseNotFoundError as exc:
            raise PermanentEventError(str(exc)) from None
        log = ctx.log.bind(case_id=str(case.id))
        if case.status is not CaseStatus.READY_TO_DRAFT:
            # Already drafted by an earlier event, cancelled, or back in intake.
            log.info("draft_skipped", status=case.status.value)
            return
        facts = await case_service.get_current_facts(ctx.session, case)
        if missing(facts):
            log.warning("draft_skipped_missing_facts")
            return

        tools = ToolContext(
            session=ctx.session,
            event=ctx.event,
            now=ctx.now,
            log=log,
            outbox=await TelegramOutbox.for_event(ctx),
            case=case,
        )
        email = await self._llm.extract_structured(
            draft_messages(today=self._today(ctx), facts=facts),
            DraftSupportEmail,
            purpose=PURPOSE_DRAFT,
        )
        await self._executor.execute(email, tools, allowed=DRAFT_TOOLS)
        await transition(
            ctx.session,
            case,
            CaseStatus.WAITING_FOR_USER_APPROVAL,
            reason="draft ready for approval",
            actor=TransitionActor.SYSTEM,
            event_id=ctx.event.id,
        )

    async def review(
        self,
        ctx: HandlerContext,
        outbox: TelegramOutbox,
        case: SupportCase,
        text: str,
        *,
        telegram_message_id: int | None,
    ) -> None:
        """Handle a chat message while the case's draft waits for approval."""
        session = ctx.session
        tools = ToolContext(
            session=session,
            event=ctx.event,
            now=ctx.now,
            log=ctx.log.bind(case_id=str(case.id)),
            outbox=outbox,
            case=case,
        )
        await case_messages.add_message(
            session,
            case,
            MessageRole.USER,
            text,
            event_id=ctx.event.id,
            telegram_message_id=telegram_message_id,
        )
        email = await drafts.live_email(session, case)
        if email is None or email.status is not OutboundEmailStatus.AWAITING_APPROVAL:
            # Waiting for approval without a draft to approve: draft again.
            tools.log.warning("draft_review_without_draft")
            await transition(
                session,
                case,
                CaseStatus.READY_TO_DRAFT,
                reason="no draft awaiting approval",
                actor=TransitionActor.SYSTEM,
                event_id=ctx.event.id,
            )
            await say(tools, REDRAFTING_REPLY)
            await request_draft(session, case, cause_event_id=ctx.event.id, now=ctx.now)
            return

        today = self._today(ctx)
        facts = await case_service.get_current_facts(session, case)
        history = await case_messages.recent_messages(session, case, limit=HISTORY_LIMIT)
        turn = _ReviewTurn(
            session=session,
            ctx=ctx,
            tools=tools,
            case=case,
            email=email,
            text=text,
            source_ref=_source_ref(ctx, telegram_message_id),
            today=today,
        )
        result = await run_turn(
            self._llm,
            self._executor,
            tools,
            # The last history entry is the message just recorded.
            messages=review_messages(
                today=today,
                facts=facts,
                to_address=email.to_address,
                subject=email.subject,
                body_text=email.body_text,
                history=history[:-1],
                latest=text,
            ),
            decision_model=DraftReviewDecision,
            allowed_tools=REVIEW_TOOLS,
            review=turn.review,
            purpose=PURPOSE_REVIEW,
        )
        await turn.conclude(result)

    def _today(self, ctx: HandlerContext) -> date:
        return ctx.now.astimezone(self._timezone).date()


def _source_ref(ctx: HandlerContext, telegram_message_id: int | None) -> str:
    if telegram_message_id is not None:
        return f"telegram:{telegram_message_id}"
    return f"event:{ctx.event.id}"


@dataclass
class _ReviewTurn:
    session: AsyncSession
    ctx: HandlerContext
    tools: ToolContext
    case: SupportCase
    email: OutboundEmail
    text: str
    source_ref: str
    today: date
    changed: list[IntakeField] = field(default_factory=list)

    async def review(self, decision: DraftReviewDecision) -> BaseModel:
        dropped: list[str] = []
        for update in decision.facts:
            value = accept_fact(update, message_text=self.text, today=self.today)
            if value is None:
                dropped.append(update.key.value)
                continue
            outcome = await record_fact(
                self.session, self.case, update.key, value, source_ref=self.source_ref
            )
            if outcome is Recorded.RECORDED:
                self.changed.append(update.key)
            elif outcome is Recorded.REJECTED:
                dropped.append(update.key.value)
        if dropped:
            # Keys only: the values are the user's words.
            self.tools.log.info("draft_review_facts_dropped", keys=dropped)

        still_missing = missing(await case_service.get_current_facts(self.session, self.case))
        if still_missing:
            # The change needs more details (e.g. a new issue type): back to intake.
            discarded = await drafts.discard_live(
                self.session, self.case, to_status=OutboundEmailStatus.SUPERSEDED
            )
            if discarded is not None:
                await retire_buttons(self.tools, discarded)
            await transition(
                self.session,
                self.case,
                CaseStatus.GATHERING_CONTEXT,
                reason="more details needed",
                actor=TransitionActor.SYSTEM,
                event_id=self.ctx.event.id,
            )
            return AskUser(tool="ask_user", question=FALLBACK_QUESTIONS[still_missing[0]])

        action = decision.action
        if isinstance(action, ReplyToUser) and self.changed:
            # A changed fact changes the email, at least its recipient or
            # signature, which code fills in. Re-issue the current text as a
            # new version so the old Send button can't approve stale details.
            return DraftSupportEmail(
                tool="draft_support_email", subject=self.email.subject, body=self.email.body_text
            )
        return action

    async def conclude(self, result: TurnResult) -> None:
        if result.outcome is TurnOutcome.TOOL_ENDED and not isinstance(
            result.final_action, ReplyToUser
        ):
            return
        # A chat reply (or a turn that ended oddly): the draft still waits, so
        # make sure it can still be approved.
        if self.case.status is CaseStatus.WAITING_FOR_USER_APPROVAL:
            await ensure_draft_buttons(self.tools, self.case)

"""The intake conversation: from a complaint to a case that is ready to draft (M5).

One user message is one agent turn (PLAN D12). The model returns the facts in
the message and a proposed action. Code then:

1. opens a case if there is none and the message describes a problem (but
   never for a chat reply while the focused case's email is with support:
   that message is about the sent case);
2. validates each fact and records it with provenance (`user_message`);
3. works out from the recorded facts what is still missing (`policies`);
4. chooses what happens: the model's question, a fallback question when the
   model wrongly thinks it is done, or, once nothing is missing, a short
   notice and a queued `DRAFT_EMAIL` event (M6). Only code moves the case
   between GATHERING_CONTEXT and READY_TO_DRAFT.
"""

from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.drafting import request_draft
from app.agent.facts import Recorded, accept_fact, missing, record_fact
from app.agent.policies import FALLBACK_QUESTIONS, IntakeField
from app.agent.prompts import SentCase, intake_context, intake_messages
from app.agent.runtime import TurnOutcome, TurnResult, run_turn
from app.agent.schemas import FinishIntake, IntakeDecision
from app.cases import messages as case_messages
from app.cases import service as case_service
from app.cases.models import CaseStatus, MessageRole, SupportCase, TransitionActor
from app.cases.state_machine import transition
from app.email import drafts
from app.email.models import OutboundEmailStatus
from app.events.handlers import HandlerContext
from app.llm.client import LLMClient
from app.telegram.delivery import TelegramOutbox
from app.tools.chat_tools import AskUser, ReplyToUser, chat_tools, say
from app.tools.registry import ToolContext, ToolExecutor

PURPOSE = "intake"
ALLOWED_TOOLS = frozenset({"ask_user", "reply_to_user"})
# How much of the case's chat the model sees.
HISTORY_LIMIT = 20

NO_CASE_REPLY = (
    "Hi! I help with problems with online orders, like missing or wrong items, damage, "
    "late deliveries or billing mistakes. Tell me what happened and I'll take it from there."
)
DRAFTING_NOTICE = "Thanks, I have everything I need. I'm drafting the email to {merchant} now."


def drafting_notice(case: SupportCase) -> str:
    merchant = f"{case.merchant_name} support" if case.merchant_name else "support"
    return DRAFTING_NOTICE.format(merchant=merchant)


class IntakeAgent:
    def __init__(self, llm: LLMClient, *, timezone: ZoneInfo) -> None:
        self._llm = llm
        self._timezone = timezone
        self._executor = ToolExecutor(chat_tools())

    async def handle(
        self,
        ctx: HandlerContext,
        outbox: TelegramOutbox,
        text: str,
        *,
        case: SupportCase | None,
        telegram_message_id: int | None,
        sent_case: SupportCase | None = None,
    ) -> None:
        """One intake turn. `case` is the routed intake case, or None to maybe open one.

        `sent_case`, with no `case`: the focused case whose email went out.
        """
        session = ctx.session
        today = ctx.now.astimezone(self._timezone).date()
        facts = await case_service.get_current_facts(session, case) if case else {}
        history = (
            await case_messages.recent_messages(session, case, limit=HISTORY_LIMIT) if case else []
        )
        context = intake_context(
            today=today,
            timezone=self._timezone.key,
            has_case=case is not None,
            facts=facts,
            missing=missing(facts),
            sent=await self._sent_summary(session, sent_case) if case is None else None,
        )
        tools = ToolContext(
            session=session,
            event=ctx.event,
            now=ctx.now,
            log=ctx.log.bind(case_id=str(case.id)) if case else ctx.log,
            outbox=outbox,
            case=case,
        )
        turn = _IntakeTurn(
            session=session,
            ctx=ctx,
            tools=tools,
            text=text,
            telegram_message_id=telegram_message_id,
            today=today,
            beside_sent_case=case is None and sent_case is not None,
        )
        if case is not None:
            await turn.record_user_message(case)

        result = await run_turn(
            self._llm,
            self._executor,
            tools,
            messages=intake_messages(context=context, history=history, latest=text),
            decision_model=IntakeDecision,
            allowed_tools=ALLOWED_TOOLS,
            review=turn.review,
            purpose=PURPOSE,
        )
        await turn.conclude(result)

        # Ready to draft: queue the draft. Also after a chat reply, so a draft
        # whose event died is retried by the user's next message.
        opened = tools.case
        if opened is not None and opened.status is CaseStatus.READY_TO_DRAFT:
            await request_draft(session, opened, cause_event_id=ctx.event.id, now=ctx.now)

    async def _sent_summary(
        self, session: AsyncSession, case: SupportCase | None
    ) -> SentCase | None:
        if case is None:
            return None
        email = await drafts.latest_version(session, case)
        if email is None or email.status is not OutboundEmailStatus.SENT:
            return None
        return SentCase(
            merchant=case.merchant_name,
            to_address=email.to_address,
            subject=email.subject,
            sent_on=email.sent_at.astimezone(self._timezone).date() if email.sent_at else None,
        )


@dataclass
class _IntakeTurn:
    """The state of one intake turn, shared by the review hook and the conclusion."""

    session: AsyncSession
    ctx: HandlerContext
    tools: ToolContext
    text: str
    telegram_message_id: int | None
    today: date
    # No case is routed, but the focused case's email is with support.
    beside_sent_case: bool = False
    facts_changed: bool = False

    @property
    def source_ref(self) -> str:
        if self.telegram_message_id is not None:
            return f"telegram:{self.telegram_message_id}"
        return f"event:{self.ctx.event.id}"

    async def record_user_message(self, case: SupportCase) -> None:
        await case_messages.add_message(
            self.session,
            case,
            MessageRole.USER,
            self.text,
            event_id=self.ctx.event.id,
            telegram_message_id=self.telegram_message_id,
        )

    async def review(self, decision: IntakeDecision) -> BaseModel:
        action = decision.action
        accepted: list[tuple[IntakeField, str | date]] = []
        dropped: list[str] = []
        for update in decision.facts:
            value = accept_fact(update, message_text=self.text, today=self.today)
            if value is None:
                dropped.append(update.key.value)
            else:
                accepted.append((update.key, value))

        case = self.tools.case
        if case is None and self.beside_sent_case and isinstance(action, ReplyToUser):
            # A chat reply is about the sent case, whatever facts came with it:
            # opening a case here would start a duplicate of it.
            if accepted:
                self.tools.log.info("intake_facts_ignored_beside_sent_case", count=len(accepted))
            return action
        if case is None:
            # Small talk opens no case.
            if not accepted and not isinstance(action, AskUser):
                if isinstance(action, ReplyToUser):
                    return action
                return ReplyToUser(tool="reply_to_user", text=NO_CASE_REPLY)
            case = await self._open_case()

        for key, value in accepted:
            outcome = await record_fact(self.session, case, key, value, source_ref=self.source_ref)
            if outcome is Recorded.RECORDED:
                self.facts_changed = True
            elif outcome is Recorded.REJECTED:
                dropped.append(key.value)
        if dropped:
            # Keys only: the values are the user's words.
            self.tools.log.info("intake_facts_dropped", keys=dropped)

        still_missing = missing(await case_service.get_current_facts(self.session, case))
        if still_missing:
            if case.status is CaseStatus.READY_TO_DRAFT:
                await self._move(case, CaseStatus.GATHERING_CONTEXT, "more details needed")
            if isinstance(action, FinishIntake):
                return AskUser(tool="ask_user", question=FALLBACK_QUESTIONS[still_missing[0]])
            return action
        # Complete. A chat reply may stand if nothing changed; otherwise finish.
        if (
            isinstance(action, ReplyToUser)
            and not self.facts_changed
            and case.status is CaseStatus.READY_TO_DRAFT
        ):
            return action
        return FinishIntake(tool="finish_intake")

    async def conclude(self, result: TurnResult) -> None:
        if result.outcome is TurnOutcome.TOOL_ENDED:
            return
        case = self.tools.case
        if case is None:
            await say(self.tools, NO_CASE_REPLY)
            return
        still_missing = missing(await case_service.get_current_facts(self.session, case))
        if still_missing:
            # Only reachable when the turn ran out of steps.
            await say(self.tools, FALLBACK_QUESTIONS[still_missing[0]])
            return
        if case.status is CaseStatus.GATHERING_CONTEXT:
            await self._move(case, CaseStatus.READY_TO_DRAFT, "all required details collected")
        await say(self.tools, drafting_notice(case))

    async def _open_case(self) -> SupportCase:
        case = await case_service.create_case(
            self.session,
            user_id=self.ctx.event.user_id,
            actor=TransitionActor.USER,
            reason="user reported a problem",
            event_id=self.ctx.event.id,
        )
        await case_service.focus_case(self.session, case)
        await self.record_user_message(case)
        self.tools.case = case
        self.tools.log = self.tools.log.bind(case_id=str(case.id))
        return case

    async def _move(self, case: SupportCase, to_status: CaseStatus, reason: str) -> None:
        await transition(
            self.session,
            case,
            to_status,
            reason=reason,
            actor=TransitionActor.SYSTEM,
            event_id=self.ctx.event.id,
        )

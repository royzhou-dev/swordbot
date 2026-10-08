"""Finding the order's receipt in Gmail and confirming it with the user (M8, PLAN D16).

The flow, all of it decided in code:

1. Intake calls `start` each turn. Once the merchant is known and the order
   number isn't, it queues one `SEARCH_RECEIPTS` event per case and merchant.
2. The event searches Gmail (`search_order_emails`), then has the model read
   the best candidate: one structured call on text already trimmed in code.
   An event reads one email, so it stays within the handler's time limit; if
   that email isn't a receipt, the next candidate gets its own event.
3. `verify_receipt` keeps only values that literally appear in the email, and
   the order is shown with [Yes] [No]. Nothing from an email becomes a case
   fact before Yes (Invariant 1), and then with provenance `gmail_receipt`.
4. No moves on to the next candidate. A support address found in a confirmed
   receipt is offered separately: [Use it] [No].

Email content is untrusted (Invariant 4): it reaches the model only inside a
data block, is never logged, and is not stored. Only the checked details are
kept, in the buttons' payload and then as facts. A Gmail failure never blocks
the case: intake just asks the user instead.
"""

import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions import service as action_service
from app.actions.models import ActionKind, PendingAction
from app.actions.service import ButtonSpec
from app.agent.facts import compact, looks_like_email
from app.agent.intake import advance
from app.agent.policies import (
    ORDER_ITEMS,
    ORDER_TOTAL,
    REQUIRED,
    IntakeField,
    Requirement,
    parse_issue_type,
)
from app.agent.prompts import receipt_messages
from app.agent.schemas import ReceiptInfo
from app.cases import messages as case_messages
from app.cases import service as case_service
from app.cases.errors import CaseNotFoundError, InvalidFactError
from app.cases.models import CaseFact, CaseStatus, FactSource, MessageRole, SupportCase
from app.email.errors import GmailError
from app.email.gmail_client import GmailClient
from app.events import service as event_service
from app.events.errors import PermanentEventError
from app.events.handlers import HandlerContext
from app.events.models import EventSource, EventType
from app.events.schemas import NewEvent, SearchReceiptsPayload
from app.llm.client import LLMClient
from app.llm.errors import InvalidAgentDecisionError, LLMRefusalError
from app.logging import get_logger
from app.telegram.delivery import TelegramOutbox
from app.tools.chat_tools import say
from app.tools.receipt_tools import (
    OrderEmailCandidate,
    OrderEmailCandidates,
    ReadEmail,
    SearchOrderEmails,
    receipt_tools,
)
from app.tools.registry import ToolContext, ToolExecutor

log = get_logger(__name__)

PURPOSE = "read_receipt"
RECEIPT_TOOLS = frozenset({"search_order_emails", "read_email"})

MAX_ORDER_NUMBER_LENGTH = 128
MAX_TOTAL_LENGTH = 40
# An order date further than this from the email's own date is not believed.
_MAX_DAYS_BEFORE_EMAIL = 45
# A label the model copied along with the number: "Order #374886", "Order number: A-1".
_ORDER_LABEL = re.compile(
    r"^(?:(?:order|confirmation|invoice)\b\s*)?(?:(?:number|num|no|id)\b\.?\s*)?[#:\s]*",
    re.IGNORECASE,
)
# Quantities and prices the model copied along with an item's name.
_QUANTITY = re.compile(
    r"(?:^|(?<=\s))(?:qty:?\s*\d+|\d+\s*[x\u00d7]|[x\u00d7]\s*\d+)(?=\s|$)", re.IGNORECASE
)
_PRICE = re.compile(r"[$\u20ac\u00a3]\s?\d[\d.,]*(?:\s?(?:USD|EUR|GBP|CAD|AUD))?")
# Addresses nobody reads.
_NO_REPLY = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|noreply|notifications?|mailer(?:-daemon)?"
    r"|bounces?|postmaster)\b",
    re.IGNORECASE,
)

FOUND_INTRO = "I found this order in your Gmail:"
FOUND_QUESTION = "Is this the order you mean?"
NOT_FOUND_NOTICE = "I looked for the order in your Gmail but couldn't find it."
NO_MORE_NOTICE = "I couldn't find another matching order in your Gmail."
REJECTED_NOTICE = "OK, not that one."
CHECKING_NEXT_REPLY = "OK, not that one. Let me check for another."
CONFIRMED_NOTICE = "Got it, I'll use that order."
CONTACT_PROMPT = (
    "Got it, I'll use that order. The receipt lists {address} for customer support. "
    "Should I write to that address?"
)
CONTACT_USED_NOTICE = "OK, I'll write to {address}."
CONTACT_SKIPPED_NOTICE = "OK, I won't use that address."
# What the chat log (and so the intake model) sees instead of the email's content.
RECEIPT_NOTE = (
    "(Showed the user an order found in their Gmail and asked whether it is the right one, "
    "with Yes/No buttons.)"
)
CONTACT_NOTE = (
    "(Asked the user, with buttons, whether to write to the support address found in the receipt.)"
)


class Receipt(BaseModel):
    """An order's details, each checked against the email it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gmail_message_id: str
    order_number: str | None = None
    order_date: date | None = None
    total: str | None = None
    items: list[str] = []
    support_email: str | None = None


class ReceiptButtonPayload(BaseModel):
    """What [Yes] and [No] under a found order carry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt: Receipt
    # Gmail message ids to try if this one is rejected.
    remaining: list[str] = []
    rejected: int = 0


class ContactButtonPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    support_email: str
    gmail_message_id: str


def verify_receipt(
    info: ReceiptInfo, email: OrderEmailCandidate, *, today: date, timezone: ZoneInfo
) -> Receipt | None:
    """Keep what the email itself shows. None if it isn't a usable receipt.

    The model read untrusted text, so every value must appear in that text
    (compared without case, spaces or `#`). A value that doesn't is dropped,
    never corrected.
    """
    if not info.is_order_receipt:
        return None
    haystack = compact(f"{email.subject}\n{email.text}")

    def shown(value: str) -> bool:
        needle = compact(value)
        return bool(needle) and needle in haystack

    order_number = _ORDER_LABEL.sub("", info.order_number or "", count=1).strip() or None
    if order_number and (len(order_number) > MAX_ORDER_NUMBER_LENGTH or not shown(order_number)):
        order_number = None
    total = info.total
    if total and (len(total) > MAX_TOTAL_LENGTH or not shown(total)):
        total = None
    items = list(dict.fromkeys(clean_item(item) for item in info.items if shown(item)))
    items = [item for item in items if item]
    if order_number is None and total is None and not items:
        # Nothing the user could recognize the order by.
        return None

    support_email = (info.support_email or "").removeprefix("mailto:").strip("<> ") or None
    if support_email and not (
        looks_like_email(support_email)
        and support_email.casefold() in f"{email.sender}\n{email.text}".casefold()
        and not _NO_REPLY.match(support_email)
    ):
        support_email = None
    return Receipt(
        gmail_message_id=email.message_id,
        order_number=order_number,
        order_date=_plausible_date(info.order_date, email, today=today, timezone=timezone),
        total=total,
        items=items,
        support_email=support_email,
    )


def clean_item(item: str) -> str:
    """An item's name and variant on one line, without quantity or price.

    Runs after the item was found in the email, so only ever removes text.
    """
    item = " - ".join(line.strip() for line in item.splitlines() if line.strip())
    item = _QUANTITY.sub(" ", _PRICE.sub(" ", item))
    return " ".join(item.split()).strip(" -,")


def _plausible_date(
    value: str | None, email: OrderEmailCandidate, *, today: date, timezone: ZoneInfo
) -> date | None:
    """Dates are printed in too many ways to find in the text, so bound them instead:
    not in the future, and shortly before the email that reports the order.
    """
    if value is None:
        return None
    try:
        order_date = date.fromisoformat(value)
    except ValueError:
        return None
    latest = today
    if email.received_at is not None:
        latest = min(today, email.received_at.astimezone(timezone).date())
    # A day's slack: the email's date is in the user's timezone, the order's may not be.
    if (
        not latest - timedelta(days=_MAX_DAYS_BEFORE_EMAIL)
        <= order_date
        <= min(today, latest + timedelta(days=1))
    ):
        return None
    return order_date


def format_receipt(merchant: str, receipt: Receipt, email: OrderEmailCandidate) -> str:
    """The message asking whether this is the order. Shows only checked details."""
    # No "#" before the number: Telegram would turn it into a hashtag link.
    lines = [FOUND_INTRO, "", f"Order from {merchant}"]
    if receipt.order_number:
        lines.append(f"Order number: {receipt.order_number}")
    if receipt.order_date:
        lines.append(f"Date: {_day(receipt.order_date)}")
    if receipt.total:
        lines.append(f"Total: {receipt.total}")
    if receipt.items:
        lines.append(f"Items: {', '.join(receipt.items)}")
    lines += ["", f'From the email "{email.subject}"', "", FOUND_QUESTION]
    return "\n".join(lines)


def _day(value: date) -> str:
    return f"{value:%b} {value.day}, {value.year}"


class ReceiptAgent:
    """The `SEARCH_RECEIPTS` handler and the buttons under a found order."""

    def __init__(self, llm: LLMClient, gmail: GmailClient, *, timezone: ZoneInfo) -> None:
        self._llm = llm
        self._gmail = gmail
        self._timezone = timezone
        self._executor = ToolExecutor(receipt_tools(gmail, timezone=timezone))

    # --- Called by intake ---------------------------------------------------------------

    async def available(self) -> bool:
        try:
            return await self._gmail.can_read()
        except GmailError as exc:
            log.warning("receipt_search_unavailable", error_type=type(exc).__name__)
            return False

    async def start(
        self,
        session: AsyncSession,
        case: SupportCase,
        facts: Mapping[str, CaseFact],
        *,
        now: datetime,
    ) -> bool:
        """Queue the search once the merchant is known and the order number isn't.

        One search per case and merchant: the event is deduplicated, so a
        later turn (or a corrected merchant) decides this again for free.
        """
        merchant = _merchant(facts)
        issue_type = parse_issue_type(_text(facts, IntakeField.ISSUE_TYPE))
        if (
            merchant is None
            or IntakeField.ORDER_NUMBER in facts
            or issue_type is None
            or Requirement.ORDER_IDENTIFIER not in REQUIRED[issue_type]
        ):
            return False
        if not await self.available():
            return False
        key = "".join(ch for ch in merchant.casefold() if ch.isalnum())[:64]
        event_id = await event_service.enqueue(
            session,
            NewEvent(
                user_id=case.user_id,
                type=EventType.SEARCH_RECEIPTS,
                source=EventSource.SYSTEM,
                external_id=f"receipts:{case.id}:{key}",
                payload=SearchReceiptsPayload(case_id=case.id).model_dump(mode="json"),
                case_id=case.id,
            ),
            now=now,
        )
        return event_id is not None

    async def awaiting_answer(
        self, session: AsyncSession, case: SupportCase, *, now: datetime
    ) -> bool:
        return await action_service.has_open_kind(
            session, case.id, ActionKind.CONFIRM_RECEIPT, now=now
        )

    # --- The SEARCH_RECEIPTS handler ----------------------------------------------------

    async def search(self, ctx: HandlerContext, payload: SearchReceiptsPayload) -> None:
        try:
            case = await case_service.get_case(
                ctx.session, payload.case_id, user_id=ctx.event.user_id
            )
        except CaseNotFoundError as exc:
            raise PermanentEventError(str(exc)) from None
        tools = ToolContext(
            session=ctx.session,
            event=ctx.event,
            now=ctx.now,
            log=ctx.log.bind(case_id=str(case.id)),
            outbox=await TelegramOutbox.for_event(ctx),
            case=case,
        )
        if case.status is not CaseStatus.GATHERING_CONTEXT:
            # Cancelled, or already past intake.
            tools.log.info("receipt_search_skipped", status=case.status.value)
            return
        facts = await case_service.get_current_facts(ctx.session, case)
        merchant = _merchant(facts)
        if merchant is None or IntakeField.ORDER_NUMBER in facts:
            # The user gave the order number meanwhile: nothing to look up.
            await advance(tools, case)
            return

        order_date = _order_date(facts)
        try:
            candidate, remaining = await self._next_candidate(tools, payload, merchant, order_date)
        except GmailError as exc:
            # The lookup is a convenience: without it, intake asks the user.
            tools.log.warning("receipt_search_failed", error_type=type(exc).__name__)
            await advance(tools, case)
            return

        receipt = None
        if candidate is not None:
            receipt = await self._read(tools, candidate, merchant, order_date)
        if candidate is not None and receipt is not None:
            await self._present(tools, case, merchant, candidate, receipt, remaining, payload)
        elif remaining:
            await _queue_next(tools, case, remaining, rejected=payload.rejected)
        else:
            notice = NO_MORE_NOTICE if payload.rejected else NOT_FOUND_NOTICE
            await advance(tools, case, preface=notice)

    async def _next_candidate(
        self,
        tools: ToolContext,
        payload: SearchReceiptsPayload,
        merchant: str,
        order_date: date | None,
    ) -> tuple[OrderEmailCandidate | None, list[str]]:
        """The email to read in this event, and the ids left after it."""
        if payload.candidates is None:
            found = await self._executor.execute(
                SearchOrderEmails(
                    tool="search_order_emails", merchant=merchant, approximate_date=order_date
                ),
                tools,
                allowed=RECEIPT_TOOLS,
            )
            if not isinstance(found, OrderEmailCandidates):
                raise TypeError(f"search_order_emails returned {type(found).__name__}")
            if not found.candidates:
                return None, []
            return found.candidates[0], [c.message_id for c in found.candidates[1:]]
        if not payload.candidates:
            return None, []
        read = await self._executor.execute(
            ReadEmail(tool="read_email", message_id=payload.candidates[0]),
            tools,
            allowed=RECEIPT_TOOLS,
        )
        if not isinstance(read, OrderEmailCandidate):
            raise TypeError(f"read_email returned {type(read).__name__}")
        return read, payload.candidates[1:]

    async def _read(
        self,
        tools: ToolContext,
        email: OrderEmailCandidate,
        merchant: str,
        order_date: date | None,
    ) -> Receipt | None:
        """One model call on one trimmed email. Unusable output counts as "not a receipt"."""
        received_on = (
            email.received_at.astimezone(self._timezone).date() if email.received_at else None
        )
        try:
            info = await self._llm.extract_structured(
                receipt_messages(
                    merchant=merchant,
                    approximate_date=order_date,
                    sender=email.sender,
                    subject=email.subject,
                    received_on=received_on,
                    text=email.text,
                ),
                ReceiptInfo,
                purpose=PURPOSE,
            )
        except (InvalidAgentDecisionError, LLMRefusalError) as exc:
            tools.log.warning("receipt_unreadable", error_type=type(exc).__name__)
            return None
        receipt = verify_receipt(
            info,
            email,
            today=tools.now.astimezone(self._timezone).date(),
            timezone=self._timezone,
        )
        tools.log.info("receipt_read", usable=receipt is not None)
        return receipt

    async def _present(
        self,
        tools: ToolContext,
        case: SupportCase,
        merchant: str,
        email: OrderEmailCandidate,
        receipt: Receipt,
        remaining: list[str],
        payload: SearchReceiptsPayload,
    ) -> None:
        button = ReceiptButtonPayload(
            receipt=receipt, remaining=remaining, rejected=payload.rejected
        ).model_dump(mode="json")
        group_id = await action_service.create_group(
            tools.session,
            user_id=case.user_id,
            buttons=[
                ButtonSpec(ActionKind.CONFIRM_RECEIPT, "Yes", button),
                ButtonSpec(ActionKind.REJECT_RECEIPT, "No", button),
            ],
            case_id=case.id,
            # Only while intake is still gathering: later the facts are settled.
            expected_case_status=CaseStatus.GATHERING_CONTEXT,
        )
        await tools.outbox.send_message(
            format_receipt(merchant, receipt, email), action_group_id=group_id
        )
        await _note(tools, case, RECEIPT_NOTE)

    # --- Buttons ------------------------------------------------------------------------

    async def confirm(self, tools: ToolContext, case: SupportCase, action: PendingAction) -> None:
        """[Yes]: the receipt's details become case facts, sourced to the email."""
        receipt = _receipt_payload(action).receipt
        ref = f"gmail:{receipt.gmail_message_id}"
        details: dict[str, object] = {
            IntakeField.ORDER_NUMBER: receipt.order_number,
            IntakeField.ORDER_DATE: receipt.order_date,
            ORDER_TOTAL: receipt.total,
            ORDER_ITEMS: ", ".join(receipt.items) or None,
        }
        for key, value in details.items():
            if value is not None:
                await _set_receipt_fact(tools, case, key, value, ref)

        facts = await case_service.get_current_facts(tools.session, case)
        if receipt.support_email and IntakeField.SUPPORT_EMAIL not in facts:
            # Asked separately: a receipt's contact is often not where support reads mail.
            await self._offer_contact(tools, case, receipt.support_email, receipt.gmail_message_id)
            return
        await advance(tools, case, preface=CONFIRMED_NOTICE)

    async def reject(self, tools: ToolContext, case: SupportCase, action: PendingAction) -> None:
        """[No]: try the next candidate, or go back to asking."""
        payload = _receipt_payload(action)
        if payload.remaining:
            await _queue_next(tools, case, payload.remaining, rejected=payload.rejected + 1)
            await say(tools, CHECKING_NEXT_REPLY)
            return
        await advance(tools, case, preface=REJECTED_NOTICE)

    async def _offer_contact(
        self, tools: ToolContext, case: SupportCase, address: str, gmail_message_id: str
    ) -> None:
        button = ContactButtonPayload(
            support_email=address, gmail_message_id=gmail_message_id
        ).model_dump(mode="json")
        group_id = await action_service.create_group(
            tools.session,
            user_id=case.user_id,
            buttons=[
                ButtonSpec(ActionKind.USE_RECEIPT_CONTACT, "Use it", button),
                ButtonSpec(ActionKind.SKIP_RECEIPT_CONTACT, "No", button),
            ],
            case_id=case.id,
            expected_case_status=CaseStatus.GATHERING_CONTEXT,
        )
        await tools.outbox.send_message(
            CONTACT_PROMPT.format(address=address), action_group_id=group_id
        )
        await _note(tools, case, CONTACT_NOTE)

    async def use_contact(
        self, tools: ToolContext, case: SupportCase, action: PendingAction
    ) -> None:
        """[Use it]: the receipt's support address becomes the case's recipient."""
        try:
            payload = ContactButtonPayload.model_validate(action.payload)
        except ValidationError:
            raise PermanentEventError(f"action {action.id} has an invalid payload") from None
        if not looks_like_email(payload.support_email):
            raise PermanentEventError(f"action {action.id} carries an invalid address")
        await _set_receipt_fact(
            tools,
            case,
            IntakeField.SUPPORT_EMAIL,
            payload.support_email,
            f"gmail:{payload.gmail_message_id}",
        )
        await advance(
            tools, case, preface=CONTACT_USED_NOTICE.format(address=payload.support_email)
        )

    async def skip_contact(self, tools: ToolContext, case: SupportCase) -> None:
        await advance(tools, case, preface=CONTACT_SKIPPED_NOTICE)


async def _queue_next(
    tools: ToolContext, case: SupportCase, candidates: list[str], *, rejected: int
) -> None:
    """Read the next candidate in its own event, deduplicated per causing event."""
    await event_service.enqueue(
        tools.session,
        NewEvent(
            user_id=case.user_id,
            type=EventType.SEARCH_RECEIPTS,
            source=EventSource.SYSTEM,
            external_id=f"receipts:{case.id}:next:{tools.event.id}",
            payload=SearchReceiptsPayload(
                case_id=case.id, candidates=candidates, rejected=rejected
            ).model_dump(mode="json"),
            case_id=case.id,
        ),
        now=tools.now,
    )


async def _set_receipt_fact(
    tools: ToolContext, case: SupportCase, key: str, value: object, source_ref: str
) -> None:
    try:
        await case_service.set_fact(
            tools.session,
            case,
            key,
            value,
            source=FactSource.GMAIL_RECEIPT,
            source_ref=source_ref,
        )
    except InvalidFactError:
        # Key only: the value came from an email.
        tools.log.warning("receipt_fact_rejected", key=key)


async def _note(tools: ToolContext, case: SupportCase, text: str) -> None:
    await case_messages.add_message(
        tools.session, case, MessageRole.ASSISTANT, text, event_id=tools.event.id
    )


def _receipt_payload(action: PendingAction) -> ReceiptButtonPayload:
    try:
        return ReceiptButtonPayload.model_validate(action.payload)
    except ValidationError:
        raise PermanentEventError(f"action {action.id} has an invalid receipt payload") from None


def _text(facts: Mapping[str, CaseFact], key: str) -> str | None:
    fact = facts.get(key)
    return fact.value if fact is not None and isinstance(fact.value, str) else None


def _merchant(facts: Mapping[str, CaseFact]) -> str | None:
    name = (_text(facts, IntakeField.MERCHANT_NAME) or "").strip()
    return name or None


def _order_date(facts: Mapping[str, CaseFact]) -> date | None:
    value = _text(facts, IntakeField.ORDER_DATE)
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None

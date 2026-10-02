"""Looking up order emails in the user's Gmail (M8).

Both tools are `READ_ONLY`: they search and read the mailbox and change
nothing. They take the `GmailClient` to use as a parameter, so M8.5 can run
them once per account.

What they return is untrusted email content, already parsed and trimmed in
code (`app.email.parsing`). It is never logged, and the executor logs neither
arguments nor results.
"""

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from app.email import receipts
from app.email.errors import GmailRejectedError
from app.email.gmail_client import GmailClient
from app.email.parsing import ParsedEmail, parse_message
from app.tools.registry import Tool, ToolContext, ToolRegistry, ToolRiskLevel


class SearchOrderEmails(BaseModel):
    """Find the emails most likely to be the receipt for an order from a merchant."""

    tool: Literal["search_order_emails"]
    merchant: str
    approximate_date: date | None


class ReadEmail(BaseModel):
    """Read one email found by `search_order_emails`."""

    tool: Literal["read_email"]
    message_id: str


class OrderEmailCandidate(BaseModel):
    """One email, trimmed to its visible text. Untrusted."""

    message_id: str
    sender: str
    subject: str
    received_at: datetime | None
    text: str


class OrderEmailCandidates(BaseModel):
    # Best first.
    candidates: list[OrderEmailCandidate]


def _candidate(email: ParsedEmail) -> OrderEmailCandidate:
    return OrderEmailCandidate(
        message_id=email.message_id,
        sender=email.sender,
        subject=email.subject,
        received_at=email.received_at,
        text=email.text,
    )


def receipt_tools(gmail: GmailClient, *, timezone: ZoneInfo) -> ToolRegistry:
    async def search(ctx: ToolContext, args: SearchOrderEmails) -> OrderEmailCandidates:
        query = receipts.build_query(
            args.merchant, approximate_date=args.approximate_date, now=ctx.now, timezone=timezone
        )
        if query is None:
            return OrderEmailCandidates(candidates=[])
        refs = await gmail.search(query, max_results=receipts.MAX_SEARCH_RESULTS)
        emails: list[ParsedEmail] = []
        for ref in refs:
            try:
                emails.append(parse_message(await gmail.get_message(ref.message_id)))
            except GmailRejectedError:
                # Deleted between the search and the read: leave it out.
                continue
        ranked = receipts.rank(
            emails, args.merchant, approximate_date=args.approximate_date, timezone=timezone
        )
        # Counts only: subjects and senders are the user's mail.
        ctx.log.info("order_emails_searched", results=len(refs), candidates=len(ranked))
        return OrderEmailCandidates(candidates=[_candidate(email) for email in ranked])

    async def read(ctx: ToolContext, args: ReadEmail) -> OrderEmailCandidate:
        return _candidate(parse_message(await gmail.get_message(args.message_id)))

    return ToolRegistry(
        [
            Tool(
                name="search_order_emails",
                description="Find the emails most likely to be an order's receipt.",
                risk=ToolRiskLevel.READ_ONLY,
                args_model=SearchOrderEmails,
                result_model=OrderEmailCandidates,
                run=search,
            ),
            Tool(
                name="read_email",
                description="Read one email from the user's mailbox.",
                risk=ToolRiskLevel.READ_ONLY,
                args_model=ReadEmail,
                result_model=OrderEmailCandidate,
                run=read,
            ),
        ]
    )

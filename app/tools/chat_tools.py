"""Tools that talk to the user in chat.

Both are `LOW_RISK_WRITE`: they only queue a Telegram message to the owner
(PLAN D10) and log it on the case. Neither can reach a merchant.

LLM-facing models carry no length or pattern constraints in their schema,
because OpenAI's strict mode rejects some of those keywords. Limits are
enforced by validators instead, which the repair retry reports to the model.
"""

from typing import Literal

from pydantic import BaseModel, field_validator

from app.cases import messages as case_messages
from app.cases.models import MessageRole
from app.tools.registry import Tool, ToolContext, ToolRegistry, ToolRiskLevel

MAX_QUESTION_LENGTH = 500
MAX_REPLY_LENGTH = 1000


def _bounded_text(value: str, limit: int) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    if len(value) > limit:
        raise ValueError(f"must be at most {limit} characters")
    return value


class AskUser(BaseModel):
    """Ask the user one short question about a missing detail."""

    tool: Literal["ask_user"]
    question: str

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        return _bounded_text(value, MAX_QUESTION_LENGTH)


class ReplyToUser(BaseModel):
    """Reply to a message that isn't about a missing detail (a greeting, thanks, a question)."""

    tool: Literal["reply_to_user"]
    text: str

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        return _bounded_text(value, MAX_REPLY_LENGTH)


class Said(BaseModel):
    """The message was queued for delivery."""


async def say(ctx: ToolContext, text: str) -> None:
    """Queue a message to the user and record it on the case, if there is one."""
    await ctx.outbox.send_message(text)
    if ctx.case is not None:
        await case_messages.add_message(
            ctx.session, ctx.case, MessageRole.ASSISTANT, text, event_id=ctx.event.id
        )


async def _ask_user(ctx: ToolContext, args: AskUser) -> Said:
    await say(ctx, args.question)
    return Said()


async def _reply_to_user(ctx: ToolContext, args: ReplyToUser) -> Said:
    await say(ctx, args.text)
    return Said()


ASK_USER = Tool(
    name="ask_user",
    description="Ask the user one short question.",
    risk=ToolRiskLevel.LOW_RISK_WRITE,
    args_model=AskUser,
    result_model=Said,
    run=_ask_user,
    terminal=True,
)

REPLY_TO_USER = Tool(
    name="reply_to_user",
    description="Reply to the user.",
    risk=ToolRiskLevel.LOW_RISK_WRITE,
    args_model=ReplyToUser,
    result_model=Said,
    run=_reply_to_user,
    terminal=True,
)


def chat_tools() -> ToolRegistry:
    return ToolRegistry([ASK_USER, REPLY_TO_USER])

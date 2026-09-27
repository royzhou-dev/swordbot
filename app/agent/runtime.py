"""The bounded agent loop (PLAN D11).

Each step is one `extract_structured` call returning a decision with an
`action`. The caller's `review` hook sees every decision first: it applies
what the decision says about the case and returns the action to take, which
may differ from the model's (code decides; the model recommends). Then:

- a `StopAction` ends the turn;
- a terminal tool (one that spoke to the user) runs and ends the turn;
- any other tool runs, its result is added to the conversation as a data
  block, and the next step begins.

At most `MAX_STEPS` steps run per event. The executor enforces each tool's
risk level, and the loop never passes an approval id, so the model alone can
never run a `REQUIRES_APPROVAL` tool.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from app.agent.prompts import render_data_block
from app.agent.schemas import StopAction
from app.llm.client import LLMClient, Message
from app.tools.registry import ToolContext, ToolExecutor

MAX_STEPS = 4


class TurnOutcome(StrEnum):
    # The review hook returned a StopAction.
    STOPPED = "stopped"
    # A terminal tool ran.
    TOOL_ENDED = "tool_ended"
    # MAX_STEPS ran without either; the caller decides what to say.
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class TurnResult:
    outcome: TurnOutcome
    steps: int
    # The action that ended the turn (None when exhausted).
    final_action: BaseModel | None


type Review[D] = Callable[[D], Awaitable[BaseModel]]


async def run_turn[D: BaseModel](
    llm: LLMClient,
    executor: ToolExecutor,
    ctx: ToolContext,
    *,
    messages: list[Message],
    decision_model: type[D],
    allowed_tools: frozenset[str],
    review: Review[D],
    purpose: str,
    max_steps: int = MAX_STEPS,
) -> TurnResult:
    conversation = list(messages)
    for step in range(1, max_steps + 1):
        decision = await llm.extract_structured(conversation, decision_model, purpose=purpose)
        action = await review(decision)
        if isinstance(action, StopAction):
            _log_step(ctx, step, action, purpose)
            return TurnResult(TurnOutcome.STOPPED, step, action)

        tool = executor.tool_for(action)
        _log_step(ctx, step, action, purpose)
        result = await executor.execute(action, ctx, allowed=allowed_tools)
        if tool.terminal:
            return TurnResult(TurnOutcome.TOOL_ENDED, step, action)
        conversation.append(Message(role="assistant", content=decision.model_dump_json()))
        conversation.append(
            Message(
                role="user",
                content=render_data_block(
                    "tool_result", f"{tool.name}: {result.model_dump_json()}"
                ),
            )
        )
    ctx.log.warning("agent_turn_exhausted", purpose=purpose, steps=max_steps)
    return TurnResult(TurnOutcome.EXHAUSTED, max_steps, None)


def _log_step(ctx: ToolContext, step: int, action: BaseModel, purpose: str) -> None:
    ctx.log.info(
        "agent_step",
        purpose=purpose,
        step=step,
        action=getattr(action, "tool", type(action).__name__),
        case_id=str(ctx.case.id) if ctx.case else None,
    )

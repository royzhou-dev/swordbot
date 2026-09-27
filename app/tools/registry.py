"""Tools the agent may use, and the executor that enforces their risk levels.

A tool's arguments model is also the action the LLM proposes: it has a `tool`
field whose Literal value is the tool's name, so an agent step's action union
is simply a union of tool argument models (PLAN D11).

Authorization lives here, in code, not in prompts. `ToolExecutor.execute`
refuses a tool that is unknown, outside the caller's allowlist, given invalid
arguments, or rated `REQUIRES_APPROVAL` without an approval record that the
`ApprovalVerifier` accepts. The agent runtime never passes an approval id, so
only button handlers (which consume an approval) can run such a tool.
"""

import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, get_args, get_origin

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.models import SupportCase
from app.events.schemas import ClaimedEvent
from app.telegram.delivery import TelegramOutbox
from app.tools.errors import (
    InvalidToolArgumentsError,
    ToolApprovalRequiredError,
    ToolNotPermittedError,
    UnknownToolError,
)


class ToolRiskLevel(StrEnum):
    # Looks something up; changes nothing.
    READ_ONLY = "read_only"
    # Changes our own state or talks to the user, never to a third party.
    LOW_RISK_WRITE = "low_risk_write"
    # Acts on the outside world for the user (sending email, accepting an offer).
    REQUIRES_APPROVAL = "requires_approval"


@dataclass(slots=True)
class ToolContext:
    """What a tool runs with: one event's transaction and its reply channel.

    `case` is mutable because an agent turn can open the case partway through.
    """

    session: AsyncSession
    event: ClaimedEvent
    now: datetime
    log: structlog.stdlib.BoundLogger
    outbox: TelegramOutbox
    case: SupportCase | None = None


@dataclass(frozen=True, slots=True)
class Tool[A: BaseModel, R: BaseModel]:
    name: str
    description: str
    risk: ToolRiskLevel
    args_model: type[A]
    result_model: type[R]
    run: Callable[[ToolContext, A], Awaitable[R]]
    # Ends the agent's turn, e.g. because it spoke to the user.
    terminal: bool = False

    def __post_init__(self) -> None:
        field = self.args_model.model_fields.get("tool")
        annotation = field.annotation if field is not None else None
        if get_origin(annotation) is not Literal or get_args(annotation) != (self.name,):
            raise ValueError(
                f"{self.args_model.__name__} needs a field `tool: Literal[{self.name!r}]`"
            )


type AnyTool = Tool[Any, Any]


class ToolRegistry:
    def __init__(self, tools: Iterable[AnyTool] = ()) -> None:
        self._tools: dict[str, AnyTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AnyTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AnyTool:
        tool = self._tools.get(name)
        if tool is None:
            raise UnknownToolError(name)
        return tool


class ApprovalVerifier(Protocol):
    """Decides whether an approval record authorizes this exact tool call."""

    async def verify(
        self, approval_id: uuid.UUID, tool: AnyTool, args: BaseModel, ctx: ToolContext
    ) -> bool: ...


class DenyAllApprovals:
    """Until approval records exist (M6), nothing is approved."""

    async def verify(
        self, approval_id: uuid.UUID, tool: AnyTool, args: BaseModel, ctx: ToolContext
    ) -> bool:
        return False


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, approvals: ApprovalVerifier | None = None) -> None:
        self._registry = registry
        self._approvals = approvals or DenyAllApprovals()

    def tool_for(self, call: BaseModel) -> AnyTool:
        name = getattr(call, "tool", None)
        if not isinstance(name, str):
            raise UnknownToolError(repr(type(call).__name__))
        return self._registry.get(name)

    async def execute(
        self,
        call: BaseModel,
        ctx: ToolContext,
        *,
        allowed: frozenset[str],
        approval_id: uuid.UUID | None = None,
    ) -> BaseModel:
        """Check that `call` may run, then run it and return its validated result."""
        tool = self.tool_for(call)
        if tool.name not in allowed:
            raise ToolNotPermittedError(tool.name)
        try:
            args = tool.args_model.model_validate(call.model_dump())
        except ValidationError as exc:
            locations = [".".join(str(p) for p in e["loc"]) for e in exc.errors()]
            raise InvalidToolArgumentsError(tool.name, locations) from None
        if tool.risk is ToolRiskLevel.REQUIRES_APPROVAL and (
            approval_id is None or not await self._approvals.verify(approval_id, tool, args, ctx)
        ):
            ctx.log.warning("tool_refused_without_approval", tool=tool.name)
            raise ToolApprovalRequiredError(tool.name)

        result: BaseModel = await tool.run(ctx, args)
        result_model: type[BaseModel] = tool.result_model
        if not isinstance(result, result_model):
            raise TypeError(f"tool {tool.name!r} returned {type(result).__name__}")
        # Arguments and results are not logged: they can contain user content.
        ctx.log.info(
            "tool_executed",
            tool=tool.name,
            risk=tool.risk.value,
            case_id=str(ctx.case.id) if ctx.case else None,
        )
        return result

"""Tool registry and executor: risk levels are enforced in code."""

import uuid
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from app.tools.errors import (
    InvalidToolArgumentsError,
    ToolApprovalRequiredError,
    ToolNotPermittedError,
    UnknownToolError,
)
from app.tools.registry import (
    AnyTool,
    Tool,
    ToolContext,
    ToolExecutor,
    ToolRegistry,
    ToolRiskLevel,
)
from tests.fakes import tool_context


class Echo(BaseModel):
    tool: Literal["echo"]
    word: str


class SendMoney(BaseModel):
    tool: Literal["send_money"]
    amount: int


class Done(BaseModel):
    ran: str


class Recorder:
    def __init__(self) -> None:
        self.runs: list[BaseModel] = []

    def tool(self, name: str, args_model: type[BaseModel], risk: ToolRiskLevel) -> AnyTool:
        async def run(ctx: ToolContext, args: Any) -> Done:
            self.runs.append(args)
            return Done(ran=name)

        return Tool(
            name=name,
            description=name,
            risk=risk,
            args_model=args_model,
            result_model=Done,
            run=run,
        )


class AllowAll:
    def __init__(self) -> None:
        self.checked: list[uuid.UUID] = []

    async def verify(
        self, approval_id: uuid.UUID, tool: AnyTool, args: BaseModel, ctx: ToolContext
    ) -> bool:
        self.checked.append(approval_id)
        return True


def _executor(recorder: Recorder, approvals: AllowAll | None = None) -> ToolExecutor:
    registry = ToolRegistry(
        [
            recorder.tool("echo", Echo, ToolRiskLevel.READ_ONLY),
            recorder.tool("send_money", SendMoney, ToolRiskLevel.REQUIRES_APPROVAL),
        ]
    )
    return ToolExecutor(registry, approvals)


ALL = frozenset({"echo", "send_money"})


async def test_a_permitted_tool_runs() -> None:
    recorder = Recorder()
    result = await _executor(recorder).execute(
        Echo(tool="echo", word="hi"), tool_context(), allowed=ALL
    )
    assert result == Done(ran="echo")
    assert recorder.runs == [Echo(tool="echo", word="hi")]


async def test_a_tool_outside_the_allowlist_is_refused() -> None:
    recorder = Recorder()
    with pytest.raises(ToolNotPermittedError):
        await _executor(recorder).execute(
            Echo(tool="echo", word="hi"), tool_context(), allowed=frozenset({"send_money"})
        )
    assert recorder.runs == []


async def test_an_unknown_tool_is_refused() -> None:
    class Mystery(BaseModel):
        tool: Literal["mystery"]

    with pytest.raises(UnknownToolError):
        await _executor(Recorder()).execute(Mystery(tool="mystery"), tool_context(), allowed=ALL)


async def test_arguments_are_revalidated() -> None:
    # Built without validation, as a bypassed check would.
    bad = Echo.model_construct(tool="echo", word=None)
    with pytest.raises(InvalidToolArgumentsError) as info:
        await _executor(Recorder()).execute(bad, tool_context(), allowed=ALL)
    assert info.value.locations == ["word"]


async def test_an_approval_tool_is_refused_without_an_approval() -> None:
    recorder = Recorder()
    approvals = AllowAll()
    with pytest.raises(ToolApprovalRequiredError):
        await _executor(recorder, approvals).execute(
            SendMoney(tool="send_money", amount=5), tool_context(), allowed=ALL
        )
    assert recorder.runs == []
    assert approvals.checked == []


async def test_by_default_no_approval_is_valid() -> None:
    recorder = Recorder()
    with pytest.raises(ToolApprovalRequiredError):
        await _executor(recorder).execute(
            SendMoney(tool="send_money", amount=5),
            tool_context(),
            allowed=ALL,
            approval_id=uuid.uuid4(),
        )
    assert recorder.runs == []


async def test_an_approval_the_verifier_accepts_lets_the_tool_run() -> None:
    recorder = Recorder()
    approvals = AllowAll()
    approval_id = uuid.uuid4()
    await _executor(recorder, approvals).execute(
        SendMoney(tool="send_money", amount=5),
        tool_context(),
        allowed=ALL,
        approval_id=approval_id,
    )
    assert approvals.checked == [approval_id]
    assert len(recorder.runs) == 1


def test_a_tool_name_must_match_its_arguments_tag() -> None:
    with pytest.raises(ValueError, match="Literal"):
        Recorder().tool("not_echo", Echo, ToolRiskLevel.READ_ONLY)


def test_a_name_registers_once() -> None:
    recorder = Recorder()
    registry = ToolRegistry([recorder.tool("echo", Echo, ToolRiskLevel.READ_ONLY)])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(recorder.tool("echo", Echo, ToolRiskLevel.READ_ONLY))

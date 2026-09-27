"""Structured-output validation and the single repair retry."""

import json

import pytest
from pydantic import BaseModel, field_validator

from app.events.errors import PermanentEventError
from app.llm.client import Message
from app.llm.errors import InvalidAgentDecisionError, LLMRefusalError
from app.llm.structured import RawOutput, extract_with_repair

SECRET_VALUE = "order-9f8e7d-private"


class Order(BaseModel):
    order_number: str
    item_count: int

    @field_validator("order_number")
    @classmethod
    def _has_prefix(cls, value: str) -> str:
        if not value.startswith("A-"):
            raise ValueError("must start with A-")
        return value


class ScriptedModel:
    """A `generate` function that replies from a list and records each prompt."""

    def __init__(self, *outputs: RawOutput) -> None:
        self.outputs = list(outputs)
        self.prompts: list[list[Message]] = []

    async def __call__(self, messages: list[Message]) -> RawOutput:
        self.prompts.append(messages)
        return self.outputs.pop(0)


PROMPT = [Message(role="user", content="extract the order")]
VALID = RawOutput(json.dumps({"order_number": "A-1", "item_count": 2}))


async def test_valid_output_needs_one_call() -> None:
    model = ScriptedModel(VALID)
    result = await extract_with_repair(model, PROMPT, Order)
    assert result.value == Order(order_number="A-1", item_count=2)
    assert result.repaired is False
    assert len(model.prompts) == 1


async def test_invalid_output_is_repaired_once() -> None:
    bad = RawOutput(json.dumps({"order_number": SECRET_VALUE, "item_count": 2}))
    model = ScriptedModel(bad, VALID)

    result = await extract_with_repair(model, PROMPT, Order)

    assert result.repaired is True
    assert result.value.order_number == "A-1"
    assert len(model.prompts) == 2
    repair = model.prompts[1]
    assert repair[: len(PROMPT)] == PROMPT
    assert repair[-2] == Message(role="assistant", content=bad.text)
    instruction = repair[-1]
    assert instruction.role == "user"
    assert "order_number" in instruction.content
    assert "must start with A-" in instruction.content
    # The instruction names the problem, not the rejected value.
    assert SECRET_VALUE not in instruction.content


async def test_output_invalid_twice_raises_with_locations_only() -> None:
    bad = RawOutput(json.dumps({"order_number": SECRET_VALUE, "item_count": "many"}))
    model = ScriptedModel(bad, bad)

    with pytest.raises(InvalidAgentDecisionError) as info:
        await extract_with_repair(model, PROMPT, Order)

    assert len(model.prompts) == 2
    assert info.value.schema_name == "Order"
    assert set(info.value.locations) == {"order_number", "item_count"}
    assert SECRET_VALUE not in str(info.value)
    assert isinstance(info.value, PermanentEventError)


@pytest.mark.parametrize(
    "bad",
    [
        RawOutput("Sure! Here is the order: A-1"),
        RawOutput('{"order_number": "A-1", "item_co', truncated=True),
    ],
    ids=["not_json", "truncated"],
)
async def test_unparseable_output_goes_through_repair(bad: RawOutput) -> None:
    model = ScriptedModel(bad, VALID)
    result = await extract_with_repair(model, PROMPT, Order)
    assert result.repaired is True
    assert len(model.prompts) == 2


async def test_truncation_asks_for_a_shorter_reply() -> None:
    model = ScriptedModel(RawOutput('{"order', truncated=True), VALID)
    await extract_with_repair(model, PROMPT, Order)
    assert "cut off" in model.prompts[1][-1].content


async def test_refusal_is_not_repaired() -> None:
    model = ScriptedModel(RawOutput("", refused=True), VALID)
    with pytest.raises(LLMRefusalError):
        await extract_with_repair(model, PROMPT, Order)
    assert len(model.prompts) == 1


async def test_refusal_on_the_repair_attempt_raises() -> None:
    model = ScriptedModel(RawOutput("nope"), RawOutput("", refused=True))
    with pytest.raises(LLMRefusalError):
        await extract_with_repair(model, PROMPT, Order)

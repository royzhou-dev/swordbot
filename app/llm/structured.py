"""Validating structured output, with one repair retry. Provider-independent.

The provider guarantees the JSON shape through a strict schema, but output can
still be invalid: truncated, not JSON, or rejected by one of the schema's own
validators. The model then gets one chance to fix it. The repair instruction
names where validation failed and why, without the offending values, and the
final error carries only the locations, so no output reaches logs or
`events.last_error`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from app.llm.client import Message
from app.llm.errors import InvalidAgentDecisionError, LLMRefusalError

_MAX_REPORTED_ERRORS = 10
_TRUNCATED = "output was cut off"


@dataclass(frozen=True)
class RawOutput:
    """One model response, before validation."""

    text: str
    refused: bool = False
    # The model stopped early (output token limit), so the JSON is likely cut off.
    truncated: bool = False


type Generate = Callable[[list[Message]], Awaitable[RawOutput]]


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    value: T
    repaired: bool


async def extract_with_repair[T: BaseModel](
    generate: Generate, messages: list[Message], schema: type[T]
) -> StructuredResult[T]:
    raw = await generate(messages)
    first = _validate(raw, schema)
    if not isinstance(first, list):
        return StructuredResult(first, repaired=False)

    repair = [
        *messages,
        Message(role="assistant", content=raw.text),
        Message(role="user", content=_repair_instruction(schema, first)),
    ]
    raw = await generate(repair)
    second = _validate(raw, schema)
    if not isinstance(second, list):
        return StructuredResult(second, repaired=True)
    raise InvalidAgentDecisionError(schema.__name__, [p.location for p in second])


@dataclass(frozen=True)
class _Problem:
    location: str
    # Pydantic's message, rendered without the input value.
    detail: str


def _validate[T: BaseModel](raw: RawOutput, schema: type[T]) -> T | list[_Problem]:
    if raw.refused:
        raise LLMRefusalError(schema.__name__)
    if raw.truncated:
        return [_Problem("(output)", _TRUNCATED)]
    try:
        return schema.model_validate_json(raw.text)
    except ValidationError as exc:
        return [
            _Problem(".".join(str(part) for part in error["loc"]) or "(root)", error["msg"])
            for error in exc.errors(include_input=False, include_url=False)
        ]


def _repair_instruction(schema: type[BaseModel], problems: list[_Problem]) -> str:
    listed = "\n".join(f"- {p.location}: {p.detail}" for p in problems[:_MAX_REPORTED_ERRORS])
    if any(p.detail == _TRUNCATED for p in problems):
        hint = "Your previous output was cut off. Reply again, more concisely."
    else:
        hint = "Your previous output did not validate."
    return (
        f"{hint} Return only a JSON object matching the {schema.__name__} schema.\n"
        f"Problems:\n{listed}"
    )

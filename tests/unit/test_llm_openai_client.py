"""The OpenAI client against mocked Responses API replies.

The SDK sends requests with `httpx2`, which respx doesn't intercept, so the
tests hand the client an `httpx2.MockTransport` instead.
"""

import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx2
import pytest
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, SecretStr
from structlog.testing import capture_logs

from app.agent.schemas import DraftReviewDecision, IntakeDecision
from app.config import Settings
from app.events.errors import PermanentEventError
from app.llm.client import Message, UnconfiguredLLMClient
from app.llm.errors import (
    InvalidAgentDecisionError,
    LLMAuthenticationError,
    LLMError,
    LLMQuotaError,
    LLMRefusalError,
    LLMRequestError,
    LLMTemporaryError,
)
from app.llm.factory import build_llm_client
from app.llm.openai_client import OpenAIClient
from app.llm.schemas import ExtractedIssue
from app.tools.email_tools import DraftSupportEmail

API_KEY = "sk-test-FakeKeyFakeKeyFakeKey"
MODEL = "gpt-test"
PROMPT_TEXT = "DoorDash forgot my fries, order 4411"
PROMPT = [
    Message(role="system", content="Extract the issue."),
    Message(role="user", content=PROMPT_TEXT),
]
ISSUE = {
    "merchant": "DoorDash",
    "issue_type": "missing_item",
    "issue_summary": "Fries were missing.",
    "desired_resolution": None,
}

type Handler = Callable[[httpx2.Request], httpx2.Response]


def _message(content: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_1",
        "status": "completed",
        "role": "assistant",
        "content": [content],
    }


def _response(
    *output: dict[str, Any],
    status: str = "completed",
    incomplete_reason: str | None = None,
) -> httpx2.Response:
    body = {
        "id": "resp_1",
        "object": "response",
        "created_at": 1_790_000_000,
        "status": status,
        "model": MODEL,
        "output": list(output),
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "error": None,
        "incomplete_details": (
            {"reason": incomplete_reason} if incomplete_reason is not None else None
        ),
        "usage": {
            "input_tokens": 30,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 12,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 42,
        },
    }
    return httpx2.Response(200, json=body)


def _text(text: str) -> httpx2.Response:
    return _response(_message({"type": "output_text", "text": text, "annotations": []}))


def _error(status: int, code: str | None, message: str = "boom") -> httpx2.Response:
    return httpx2.Response(
        status, json={"error": {"message": message, "type": "error", "code": code}}
    )


class Recorder:
    """Replies from a list and records each request body."""

    def __init__(self, *replies: httpx2.Response | Exception) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
async def make_client() -> AsyncIterator[Callable[[Recorder], OpenAIClient]]:
    clients: list[OpenAIClient] = []

    def make(recorder: Recorder) -> OpenAIClient:
        client = OpenAIClient(
            SecretStr(API_KEY),
            MODEL,
            timeout=5,
            max_retries=0,
            base_url="https://openai.test/v1",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(recorder)),
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


async def test_extracts_a_validated_model(make_client: Callable[[Recorder], OpenAIClient]) -> None:
    recorder = Recorder(_text(json.dumps(ISSUE)))
    client = make_client(recorder)

    issue = await client.extract_structured(PROMPT, ExtractedIssue, purpose="test")

    assert issue == ExtractedIssue.model_validate(ISSUE)
    (body,) = recorder.bodies
    assert body["model"] == MODEL
    assert body["store"] is False
    assert body["input"] == [m.model_dump() for m in PROMPT]
    fmt = body["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["name"] == "ExtractedIssue"
    assert fmt["strict"] is True
    # Strict mode: every field required, nothing extra allowed.
    assert set(fmt["schema"]["required"]) == set(ISSUE)
    assert fmt["schema"]["additionalProperties"] is False


async def test_complete_returns_text_without_a_schema(
    make_client: Callable[[Recorder], OpenAIClient],
) -> None:
    recorder = Recorder(_text("Sorry about the fries!"))
    client = make_client(recorder)

    assert await client.complete(PROMPT, purpose="test") == "Sorry about the fries!"
    assert "text" not in recorder.bodies[0]
    assert recorder.bodies[0]["store"] is False


async def test_invalid_output_is_repaired_with_a_second_request(
    make_client: Callable[[Recorder], OpenAIClient],
) -> None:
    recorder = Recorder(_text('{"merchant": "DoorDash"}'), _text(json.dumps(ISSUE)))
    client = make_client(recorder)

    with capture_logs() as logs:
        issue = await client.extract_structured(PROMPT, ExtractedIssue, purpose="test")

    assert issue.merchant == "DoorDash"
    assert len(recorder.bodies) == 2
    assert recorder.bodies[1]["input"][-1]["role"] == "user"
    (call,) = [entry for entry in logs if entry["event"] == "llm_call"]
    assert call["repaired"] is True
    assert call["calls"] == 2
    assert call["usage"] == {"input": 60, "output": 24}


async def test_output_invalid_twice_raises(
    make_client: Callable[[Recorder], OpenAIClient],
) -> None:
    client = make_client(Recorder(_text("not json"), _text("still not json")))
    with pytest.raises(InvalidAgentDecisionError):
        await client.extract_structured(PROMPT, ExtractedIssue, purpose="test")


async def test_truncated_output_is_repaired(
    make_client: Callable[[Recorder], OpenAIClient],
) -> None:
    truncated = _response(
        _message({"type": "output_text", "text": '{"merchant": "Door', "annotations": []}),
        status="incomplete",
        incomplete_reason="max_output_tokens",
    )
    recorder = Recorder(truncated, _text(json.dumps(ISSUE)))
    issue = await make_client(recorder).extract_structured(PROMPT, ExtractedIssue, purpose="test")
    assert issue.merchant == "DoorDash"
    assert "cut off" in recorder.bodies[1]["input"][-1]["content"]


@pytest.mark.parametrize("method", ["extract", "complete"])
async def test_refusal_raises(make_client: Callable[[Recorder], OpenAIClient], method: str) -> None:
    refusal = _response(_message({"type": "refusal", "refusal": "I can't help with that."}))
    recorder = Recorder(refusal)
    client = make_client(recorder)
    with pytest.raises(LLMRefusalError):
        if method == "extract":
            await client.extract_structured(PROMPT, ExtractedIssue, purpose="test")
        else:
            await client.complete(PROMPT, purpose="test")
    assert len(recorder.bodies) == 1


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (_error(401, "invalid_api_key"), LLMAuthenticationError),
        (_error(403, "unsupported_country_region_territory"), LLMAuthenticationError),
        (_error(429, "rate_limit_exceeded"), LLMTemporaryError),
        (_error(429, "insufficient_quota"), LLMQuotaError),
        (_error(400, "invalid_request_error"), LLMRequestError),
        (_error(404, "model_not_found"), LLMRequestError),
        (_error(500, None), LLMTemporaryError),
        (_error(503, None), LLMTemporaryError),
        (httpx2.ConnectError("connection refused"), LLMTemporaryError),
        (httpx2.ReadTimeout("timed out"), LLMTemporaryError),
    ],
    ids=[
        "401",
        "403",
        "429_rate",
        "429_quota",
        "400",
        "404",
        "500",
        "503",
        "connect",
        "timeout",
    ],
)
async def test_errors_are_mapped_to_typed_errors(
    make_client: Callable[[Recorder], OpenAIClient],
    reply: httpx2.Response | Exception,
    expected: type[LLMError],
) -> None:
    client = make_client(Recorder(reply))
    with pytest.raises(expected) as info:
        await client.extract_structured(PROMPT, ExtractedIssue, purpose="test")
    error = info.value
    assert type(error) is expected
    assert isinstance(error, PermanentEventError) == (expected is not LLMTemporaryError)
    # Nothing sensitive in the message that ends up in `events.last_error`, and
    # no chained SDK exception carrying the provider's response body.
    assert API_KEY not in str(error)
    assert PROMPT_TEXT not in str(error)
    assert "boom" not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


async def test_request_error_keeps_status_and_code(
    make_client: Callable[[Recorder], OpenAIClient],
) -> None:
    client = make_client(Recorder(_error(404, "model_not_found")))
    with pytest.raises(LLMRequestError) as info:
        await client.complete(PROMPT, purpose="test")
    assert info.value.status_code == 404
    assert info.value.code == "model_not_found"


async def test_call_log_has_metadata_only(
    make_client: Callable[[Recorder], OpenAIClient],
) -> None:
    client = make_client(Recorder(_text(json.dumps(ISSUE))))
    with capture_logs() as logs:
        await client.extract_structured(PROMPT, ExtractedIssue, purpose="intake_extract")

    (call,) = [entry for entry in logs if entry["event"] == "llm_call"]
    assert call["purpose"] == "intake_extract"
    assert call["model"] == MODEL
    assert call["schema"] == "ExtractedIssue"
    assert call["repaired"] is False
    assert call["usage"] == {"input": 30, "output": 12}
    assert isinstance(call["duration_ms"], int)
    logged = str(logs)
    assert PROMPT_TEXT not in logged
    assert "Fries were missing" not in logged
    assert API_KEY not in logged


async def test_failed_structured_call_is_still_logged(
    make_client: Callable[[Recorder], OpenAIClient],
) -> None:
    client = make_client(Recorder(_text("nope"), _error(500, None)))
    with capture_logs() as logs, pytest.raises(LLMTemporaryError):
        await client.extract_structured(PROMPT, ExtractedIssue, purpose="test")
    (call,) = [entry for entry in logs if entry["event"] == "llm_call"]
    assert call["calls"] == 2
    assert call["usage"] == {"input": 30, "output": 12}


async def test_unconfigured_client_fails_permanently() -> None:
    client = UnconfiguredLLMClient()
    with pytest.raises(LLMAuthenticationError):
        await client.complete(PROMPT, purpose="test")
    with pytest.raises(LLMAuthenticationError):
        await client.extract_structured(PROMPT, ExtractedIssue, purpose="test")


async def test_factory_picks_the_client_from_settings() -> None:
    unconfigured = build_llm_client(Settings(_env_file=None, openai_api_key=None))
    assert isinstance(unconfigured, UnconfiguredLLMClient)
    # An empty `OPENAI_API_KEY=` line in .env.
    blank = build_llm_client(Settings(_env_file=None, openai_api_key=" "))
    assert isinstance(blank, UnconfiguredLLMClient)

    configured = build_llm_client(Settings(_env_file=None, openai_api_key=API_KEY))
    assert isinstance(configured, OpenAIClient)
    await configured.aclose()


class _Anything(BaseModel):
    value: str


async def test_schema_name_is_the_model_name(
    make_client: Callable[[Recorder], OpenAIClient],
) -> None:
    recorder = Recorder(_text('{"value": "x"}'))
    await make_client(recorder).extract_structured(PROMPT, _Anything, purpose="test")
    assert recorder.bodies[0]["text"]["format"]["name"] == "_Anything"


def _objects(node: Any) -> Iterator[dict[str, Any]]:
    """Every object schema inside a JSON schema."""
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from _objects(item)


@pytest.mark.parametrize("schema", [IntakeDecision, DraftReviewDecision, DraftSupportEmail])
def test_agent_schemas_fit_strict_mode(schema: type[BaseModel]) -> None:
    # PLAN D11: strict mode rejects oneOf, and every field must be required.
    converted = to_strict_json_schema(schema)
    text = json.dumps(converted)
    assert '"oneOf"' not in text
    assert '"default"' not in text
    for obj in _objects(converted):
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])

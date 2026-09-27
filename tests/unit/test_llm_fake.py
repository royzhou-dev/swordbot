"""The fake LLM client that M5+ tests script."""

import pytest

from app.llm.client import Message
from app.llm.errors import InvalidAgentDecisionError, LLMTemporaryError
from app.llm.schemas import ExtractedIssue
from tests.fakes import FakeLLMClient

PROMPT = [Message(role="user", content="my order is wrong")]


async def test_replies_in_order_and_records_calls() -> None:
    llm = FakeLLMClient()
    issue = ExtractedIssue(
        merchant="DoorDash", issue_type=None, issue_summary=None, desired_resolution=None
    )
    llm.script(issue, "Which items were missing?")

    assert await llm.extract_structured(PROMPT, ExtractedIssue, purpose="extract") is issue
    assert await llm.complete(PROMPT, purpose="ask") == "Which items were missing?"
    assert [(c.method, c.schema, c.purpose) for c in llm.calls] == [
        ("extract_structured", ExtractedIssue, "extract"),
        ("complete", None, "ask"),
    ]
    assert llm.calls[0].messages == PROMPT
    assert llm.unused_replies == 0


async def test_dict_replies_are_validated_against_the_schema() -> None:
    llm = FakeLLMClient()
    llm.script(
        {
            "merchant": "Amazon",
            "issue_type": None,
            "issue_summary": None,
            "desired_resolution": None,
        },
        {"merchant": "Amazon"},
    )
    issue = await llm.extract_structured(PROMPT, ExtractedIssue, purpose="extract")
    assert issue.merchant == "Amazon"
    with pytest.raises(InvalidAgentDecisionError) as info:
        await llm.extract_structured(PROMPT, ExtractedIssue, purpose="extract")
    assert "issue_type" in info.value.locations


async def test_scripted_exceptions_are_raised() -> None:
    llm = FakeLLMClient()
    llm.script(LLMTemporaryError("503"))
    with pytest.raises(LLMTemporaryError):
        await llm.complete(PROMPT, purpose="ask")


async def test_unscripted_or_mismatched_calls_fail_loudly() -> None:
    llm = FakeLLMClient()
    with pytest.raises(AssertionError, match="unscripted"):
        await llm.complete(PROMPT, purpose="ask")
    llm.script("plain text")
    with pytest.raises(AssertionError, match="ExtractedIssue"):
        await llm.extract_structured(PROMPT, ExtractedIssue, purpose="extract")

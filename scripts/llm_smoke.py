"""Check the LLM setup against the real OpenAI API.

    uv run python scripts/llm_smoke.py
    uv run python scripts/llm_smoke.py --text "Amazon sent me the wrong charger"
    uv run python scripts/llm_smoke.py --intake
    uv run python scripts/llm_smoke.py --draft

Extracts an `ExtractedIssue` from a complaint and prints it. With `--intake`
it runs the real intake prompt instead and prints the `IntakeDecision`, which
checks that OpenAI accepts the agent's decision schema. With `--draft` it
drafts an email for a sample case and prints the `DraftSupportEmail` (the
subject and body the user would review, before the code-added sign-off). The `llm_call` log
line shows the model, token usage and whether a repair retry was needed. Uses
OPENAI_API_KEY and OPENAI_MODEL. Each run costs one or two small API calls.
"""

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.policies import missing_requirements
from app.agent.prompts import draft_messages, intake_context, intake_messages
from app.agent.schemas import IntakeDecision
from app.cases.models import CaseFact, FactSource
from app.config import Settings, get_settings
from app.llm.client import Message
from app.llm.errors import LLMError
from app.llm.factory import build_llm_client
from app.llm.schemas import ExtractedIssue
from app.logging import configure_logging
from app.tools.email_tools import DraftSupportEmail

DEFAULT_COMPLAINT = (
    "My DoorDash order from last night was missing the large fries and a Coke. "
    "I'd like a refund for the missing items."
)

SYSTEM_PROMPT = (
    "You extract details of a customer-support problem from the user's message. "
    "Only report what the user actually said. Leave a field null if it was not stated."
)


def _extraction(text: str) -> tuple[list[Message], type[BaseModel]]:
    return [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content=text),
    ], ExtractedIssue


def _intake(text: str, settings: Settings) -> tuple[list[Message], type[BaseModel]]:
    """The first intake step for a new complaint, as the app would send it."""
    context = intake_context(
        today=datetime.now(settings.user_zoneinfo).date(),
        timezone=settings.user_timezone,
        has_case=False,
        facts={},
        missing=missing_requirements([], None),
    )
    return intake_messages(context=context, history=[], latest=text), IntakeDecision


def _draft(settings: Settings) -> tuple[list[Message], type[BaseModel]]:
    """The first draft for a sample missing-item case, as the app would request it."""
    today = datetime.now(settings.user_zoneinfo).date()
    sample = {
        "merchant_name": "DoorDash",
        "issue_type": "missing_item",
        "issue_summary": "The order arrived without the large fries and the Coke.",
        "order_number": "A1B2C3",
        "order_date": (today - timedelta(days=1)).isoformat(),
        "missing_items": "large fries, Coke",
        "desired_resolution": "refund for the missing items",
        "support_email": "support@doordash.com",
    }
    facts = {
        key: CaseFact(key=key, value=value, source=FactSource.USER_MESSAGE)
        for key, value in sample.items()
    }
    return draft_messages(today=today, facts=facts), DraftSupportEmail


async def _main(text: str, *, intake: bool, draft: bool) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=False)
    if settings.openai_api_key is None:
        print("error: set OPENAI_API_KEY in .env", file=sys.stderr)
        return 2

    if draft:
        messages, schema = _draft(settings)
    elif intake:
        messages, schema = _intake(text, settings)
    else:
        messages, schema = _extraction(text)
    client = build_llm_client(settings)
    try:
        result = await client.extract_structured(messages, schema, purpose="smoke_test")
    except LLMError as exc:
        # Typed errors carry only a status and error code, never the key or prompt.
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()
    print(result.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check the LLM setup against the real API.")
    parser.add_argument("--text", default=DEFAULT_COMPLAINT, help="the complaint to extract")
    parser.add_argument(
        "--intake", action="store_true", help="run the intake prompt and IntakeDecision schema"
    )
    parser.add_argument(
        "--draft", action="store_true", help="draft an email for a sample case (DraftSupportEmail)"
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.text, intake=args.intake, draft=args.draft)))
